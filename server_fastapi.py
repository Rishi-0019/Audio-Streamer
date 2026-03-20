
import os
import socket
import json
import base64
from io import BytesIO
from typing import Set

import qrcode
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

APP_HOST = "0.0.0.0"
APP_PORT = 8000
WWW_DIR = "www"

app = FastAPI(title="LAN Audio Streamer")

# Allow CORS to make life easier when accessing pages from different origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET","POST","OPTIONS"],
    allow_headers=["*"],
    allow_credentials=True,
)

# WebSocket sets
senders: Set[WebSocket] = set()
receivers: Set[WebSocket] = set()

def get_local_ip() -> str:
    """Try to determine a LAN IPv4 address without sending traffic."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # connecting to public IP does not send packets but reveals local outbound IP
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip

def make_qr_base64(url: str) -> str:
    img = qrcode.make(url)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")

@app.get("/api/qr")
async def api_qr(request: Request, host: str = None):
    """
    Return JSON with receiver URL and QR code (base64).
    Optional query param: ?host=http://192.168.43.1:8000 to override detection.
    """
    try:
        if host:
            base = host.rstrip("/")
            detected_ip = host.split(":")[0]
            print(f"[api/qr] host override: {base}")
        else:
            detected_ip = get_local_ip()
            base = f"http://{detected_ip}:{APP_PORT}"
            print(f"[api/qr] auto-detected base: {base}")

        receiver_url = f"{base}/receiver.html"
        qr_b64 = make_qr_base64(receiver_url)

        client = request.client.host if request.client else "unknown"
        print(f"[api/qr] request from {client}; url {receiver_url}")
        return JSONResponse(content={"url": receiver_url, "qr_code": qr_b64, "ip": detected_ip})
    except Exception as e:
        print("[api/qr] ERROR:", e)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/discover", response_class=HTMLResponse)
async def discover():
    ip = get_local_ip()
    receiver_url = f"http://{ip}:{APP_PORT}/receiver.html"
    mdns_name = "lan-audio.local"
    mdns_url = f"http://{mdns_name}:{APP_PORT}/receiver.html"
    qr_b64 = make_qr_base64(receiver_url)
    html = f"""
    <!doctype html>
    <html>
      <head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1" />
      <title>Discover LAN Audio Stream</title>
      <style>body{{font-family:system-ui,Segoe UI,Roboto,Arial;padding:18px;max-width:720px;margin:auto}}
      .card{{background:#fff;padding:16px;border-radius:8px;box-shadow:0 1px 6px rgba(0,0,0,0.08)}}
      img.qr{{width:220px;height:220px;border:1px solid #eee;padding:8px;border-radius:8px}}</style>
      </head>
      <body>
        <div class="card">
          <h1>Discover & Connect</h1>
          <p>Open the receiver page on any device on the same network.</p>
          <p><strong>Direct IP link:</strong></p>
          <p><a href="{receiver_url}" target="_blank">{receiver_url}</a></p>
          <p><strong>mDNS / .local (if supported):</strong> <a href="{mdns_url}">{mdns_url}</a></p>
          <hr/>
          <p><strong>Scan this QR:</strong></p>
          <p><img class="qr" src="data:image/png;base64,{qr_b64}" alt="QR" /></p>
          <hr/>
          <h4>Notes</h4>
          <ul>
            <li>Make sure the scanning device is on the same network as this machine.</li>
            <li>Some mobile hotspots block client-to-client traffic; if scan fails, try router/hotspot settings.</li>
          </ul>
        </div>
      </body>
    </html>
    """
    return HTMLResponse(content=html)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    is_sender = False
    print("[ws] accepted connection")
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            t = msg.get("type")
            if t == "sender":
                is_sender = True
                senders.add(websocket)
                receivers.discard(websocket)
                print("[ws] identified as sender. senders:", len(senders))
            elif t == "join":
                receivers.add(websocket)
                senders.discard(websocket)
                print("[ws] receiver joined; notifying senders")
                for s in list(senders):
                    try:
                        await s.send_text(json.dumps({"type":"receiver_joined"}))
                    except:
                        senders.discard(s)
            elif t == "offer":
                # forward offers from sender to all receivers
                for r in list(receivers):
                    try:
                        await r.send_text(raw)
                    except:
                        receivers.discard(r)
            elif t == "answer":
                # forward answer from receiver to all senders
                for s in list(senders):
                    try:
                        await s.send_text(raw)
                    except:
                        senders.discard(s)
            elif t == "ice":
                # forward ICE between roles
                if is_sender or websocket in senders:
                    for r in list(receivers):
                        try: await r.send_text(raw)
                        except: receivers.discard(r)
                else:
                    for s in list(senders):
                        try: await s.send_text(raw)
                        except: senders.discard(s)
            elif t == "ping":
                await websocket.send_text(json.dumps({"type":"pong"}))
    except WebSocketDisconnect:
        print("[ws] disconnected")
    finally:
        senders.discard(websocket)
        receivers.discard(websocket)
        print("[ws] cleanup: senders", len(senders), "receivers", len(receivers))

# Serve static files via explicit GET route(s) so websockets are not intercepted
@app.get("/{file_path:path}")
async def serve_static(file_path: str):
    # Reject special API/websocket paths
    if file_path.startswith("api/") or file_path.startswith("ws") or file_path == "discover":
        raise HTTPException(status_code=404, detail="Not found")

    # default to sender.html
    if not file_path or file_path == "/":
        file_path = "sender.html"

    full_path = os.path.join(WWW_DIR, file_path)

    if os.path.isfile(full_path):
        return FileResponse(full_path)

    # directory index fallback
    if os.path.isdir(full_path):
        index_path = os.path.join(full_path, "index.html")
        if os.path.isfile(index_path):
            return FileResponse(index_path)

    # fallback: sender.html if present
    fallback = os.path.join(WWW_DIR, "sender.html")
    if os.path.isfile(fallback):
        return FileResponse(fallback)

    raise HTTPException(status_code=404, detail="File not found. Place ./www/sender.html")

if __name__ == "__main__":
    print(f"Serving ./www on http://{APP_HOST}:{APP_PORT}")
    print("Open /discover to get a QR and local link for receivers")
    uvicorn.run("server_fastapi:app", host=APP_HOST, port=APP_PORT, log_level="info")