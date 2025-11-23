# server_fastapi.py
# FastAPI static file server + /discover route (QR + local link)
# Usage:
#   pip install -r requirements.txt
#   python server_fastapi.py
#
# Put sender.html and receiver.html inside ./www/

import socket
from io import BytesIO
import base64

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import os
import qrcode
import uvicorn
import json
from typing import Set, Optional

APP_HOST = "0.0.0.0"
APP_PORT = 8000

app = FastAPI()

# WebSocket connection management
senders: Set[WebSocket] = set()
receivers: Set[WebSocket] = set()

def get_local_ip() -> str:
    """
    Attempt to return an IPv4 address that other devices on the same LAN can use.
    Tries to use default route without requiring external connectivity.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # connect to a public IP (no traffic actually sent), used to determine outbound IP
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        # fallback to localhost
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip

def make_qr_base64(url: str) -> str:
    qr = qrcode.make(url)
    buf = BytesIO()
    qr.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return b64

@app.get("/api/qr")
async def get_qr_code():
    """API endpoint to get receiver URL and QR code"""
    try:
        ip = get_local_ip()
        receiver_url = f"http://{ip}:{APP_PORT}/receiver.html"
        qr_b64 = make_qr_base64(receiver_url)
        return JSONResponse(content={
            "url": receiver_url,
            "qr_code": qr_b64,
            "ip": ip
        })
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )

@app.get("/discover", response_class=HTMLResponse)
async def discover():
    """
    Show a small page with:
     - local IP clickable link to receiver page
     - recommended .local link (mDNS) to try
     - QR code for quick scanning
     - small notes for hotspot/Wi-Fi
    """
    ip = get_local_ip()
    # Receiver page url
    receiver_url = f"http://{ip}:{APP_PORT}/receiver.html"
    # mDNS suggestion (works on many devices; may not work on all Android builds)
    mdns_name = "lan-audio.local"
    mdns_url = f"http://{mdns_name}:{APP_PORT}/receiver.html"
    qr_b64 = make_qr_base64(receiver_url)

    html = f"""
    <html>
      <head>
        <meta charset="utf-8"/>
        <title>Discover LAN Audio Stream</title>
        <meta name="viewport" content="width=device-width,initial-scale=1" />
        <style>
          body{{font-family:system-ui,Segoe UI,Roboto,Arial;padding:18px;max-width:680px;margin:auto}}
          .card{{background:#fff;padding:16px;border-radius:8px;box-shadow:0 1px 6px rgba(0,0,0,0.08)}}
          h1{{margin-top:0}}
          pre{{background:#f3f3f3;padding:10px;border-radius:6px;overflow:auto}}
          img.qr{{width:220px;height:220px;border:1px solid #eee;padding:8px;border-radius:8px}}
          a.big{{display:inline-block;margin-top:8px;padding:10px 14px;background:#0b63d0;color:#fff;border-radius:6px;text-decoration:none}}
        </style>
      </head>
      <body>
        <div class="card">
          <h1>Discover & Connect</h1>
          <p>Open the receiver page on any device on the same network (hotspot or Wi-Fi).</p>

          <p><strong>Direct IP link (recommended):</strong></p>
          <p><a class="big" href="{receiver_url}" target="_blank">{receiver_url}</a></p>

          <p><strong>mDNS / .local (try this if your device supports Bonjour):</strong></p>
          <p><a href="{mdns_url}" target="_blank">{mdns_url}</a> &nbsp; <small>(try this if your device resolves .local names)</small></p>

          <hr/>

          <p><strong>Scan this QR with your phone / tablet:</strong></p>
          <p><img class="qr" src="data:image/png;base64,{qr_b64}" alt="QR code" /></p>

          <hr/>
          <h4>Notes</h4>
          <ul>
            <li>If scanning QR doesn't work, tap the direct IP link above.</li>
            <li>If you are using a mobile hotspot, your laptop is typically the gateway IP (use that link).</li>
            <li>If your device does not support <code>.local</code> names, use the IP link or scan the QR.</li>
          </ul>

          <h4>Quick troubleshooting</h4>
          <ul>
            <li>Make sure your phone is connected to the same hotspot/router as this machine.</li>
            <li>If the page doesn't load, check your firewall and that this Python server is running.</li>
            <li>If capture (sender) shows no audio, use Chrome/Edge and choose <em>Entire screen + Share audio</em>.</li>
          </ul>
        </div>
      </body>
    </html>
    """
    return HTMLResponse(content=html)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for WebRTC signaling.
    Handles connections from both senders and receivers.
    Simple broadcast model: one sender, multiple receivers.
    """
    await websocket.accept()
    is_sender = False
    
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                msg_type = msg.get("type")
                
                if msg_type == "sender":
                    # Client explicitly identifies as sender
                    is_sender = True
                    senders.add(websocket)
                    receivers.discard(websocket)
                    
                elif msg_type == "join":
                    # Receiver wants to join - mark as receiver and notify senders
                    if not is_sender:
                        receivers.add(websocket)
                        senders.discard(websocket)
                    
                    # Notify all senders that a receiver joined
                    for sender in list(senders):
                        try:
                            await sender.send_text(json.dumps({"type": "receiver_joined"}))
                        except:
                            senders.discard(sender)
                    
                elif msg_type == "offer":
                    # Sender sends offer - mark as sender and forward to all receivers
                    if not is_sender:
                        is_sender = True
                        senders.add(websocket)
                        receivers.discard(websocket)
                    
                    # Forward offer to all receivers
                    for receiver in list(receivers):
                        try:
                            await receiver.send_text(data)
                        except:
                            receivers.discard(receiver)
                            
                elif msg_type == "answer":
                    # Receiver sends answer - forward to all senders
                    if is_sender:
                        # Shouldn't happen, but handle gracefully
                        pass
                    else:
                        receivers.add(websocket)
                        senders.discard(websocket)
                    
                    # Forward answer to all senders
                    for sender in list(senders):
                        try:
                            await sender.send_text(data)
                        except:
                            senders.discard(sender)
                            
                elif msg_type == "ice":
                    # ICE candidate - forward based on role
                    if is_sender or websocket in senders:
                        # Forward to all receivers
                        for receiver in list(receivers):
                            try:
                                await receiver.send_text(data)
                            except:
                                receivers.discard(receiver)
                    else:
                        # Forward to all senders
                        for sender in list(senders):
                            try:
                                await sender.send_text(data)
                            except:
                                senders.discard(sender)
                            
                elif msg_type == "ping":
                    # Keep-alive ping, respond with pong
                    await websocket.send_text(json.dumps({"type": "pong"}))
                    
            except json.JSONDecodeError:
                # Invalid JSON, ignore
                pass
                
    except WebSocketDisconnect:
        pass
    finally:
        # Clean up on disconnect
        senders.discard(websocket)
        receivers.discard(websocket)

# Serve static files with a catch-all route that excludes API routes
@app.get("/{file_path:path}")
async def serve_static(file_path: str):
    """Serve static files, excluding API routes and discover"""
    # Skip API routes and discover - these are handled by specific routes above
    if file_path.startswith("api/") or file_path == "discover" or file_path.startswith("ws"):
        raise HTTPException(status_code=404, detail="Not found")
    
    # Build file path
    if not file_path or file_path == "/":
        file_path = "sender.html"
    
    full_path = os.path.join("www", file_path)
    
    # Check if file exists
    if os.path.isfile(full_path):
        return FileResponse(full_path)
    
    # Try index.html for directories
    if os.path.isdir(full_path):
        index_path = os.path.join(full_path, "index.html")
        if os.path.isfile(index_path):
            return FileResponse(index_path)
    
    # Fallback to sender.html
    sender_path = os.path.join("www", "sender.html")
    if os.path.isfile(sender_path):
        return FileResponse(sender_path)
    
    raise HTTPException(status_code=404, detail="File not found")

if __name__ == "__main__":
    print(f"Serving ./www on http://{APP_HOST}:{APP_PORT}")
    print("Open /discover to get a QR and local link for receivers")
    uvicorn.run("server_fastapi:app", host=APP_HOST, port=APP_PORT, log_level="info")
