# app.py
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, BackgroundTasks, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
import secrets
import asyncio
import os
from datetime import datetime, timedelta
from typing import Dict, List
import json
import logging
import uuid

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Direct File Transfer", version="1.0.0")

# Create directories if they don't exist
os.makedirs("static", exist_ok=True)
os.makedirs("templates", exist_ok=True)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# In-memory storage for active transfers
active_transfers: Dict[str, Dict] = {}

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        self.active_connections[client_id] = websocket
        logger.info(f"Client {client_id} connected")

    def disconnect(self, client_id: str):
        if client_id in self.active_connections:
            del self.active_connections[client_id]
            logger.info(f"Client {client_id} disconnected")

    async def send_personal_message(self, message: dict, client_id: str):
        if client_id in self.active_connections:
            try:
                await self.active_connections[client_id].send_json(message)
            except Exception as e:
                logger.error(f"Error sending message to {client_id}: {e}")

manager = ConnectionManager()

def generate_otp(length=6):
    """Generate a random OTP"""
    return ''.join(secrets.choice("0123456789") for _ in range(length))

def cleanup_expired_transfers():
    """Remove expired transfers"""
    current_time = datetime.now()
    expired_otps = []
    
    for otp, data in active_transfers.items():
        if current_time > data['expires_at']:
            expired_otps.append(otp)
    
    for otp in expired_otps:
        if otp in active_transfers:
            del active_transfers[otp]
        logger.info(f"Cleaned up expired OTP: {otp}")

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/generate_otp")
async def create_otp(background_tasks: BackgroundTasks):
    """Generate a new OTP for file transfer"""
    cleanup_expired_transfers()  # Clean up expired transfers first
    
    otp = generate_otp()
    expires_at = datetime.now() + timedelta(minutes=10)
    
    active_transfers[otp] = {
        'status': 'pending',
        'expires_at': expires_at,
        'files': [],
        'created_at': datetime.now(),
        'sender_id': None,
        'receiver_id': None
    }
    
    # Schedule cleanup
    background_tasks.add_task(cleanup_expired_transfers)
    
    return JSONResponse({
        'otp': otp, 
        'expires_at': expires_at.isoformat(),
        'message': 'OTP generated successfully. Share this with the recipient.'
    })

@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    await manager.connect(websocket, client_id)
    try:
        while True:
            data = await websocket.receive_json()
            message_type = data.get('type')
            
            if message_type == 'register_otp':
                otp = data.get('otp')
                role = data.get('role')  # 'sender' or 'receiver'
                
                if otp not in active_transfers:
                    await websocket.send_json({
                        'type': 'error',
                        'message': 'Invalid OTP. Please check and try again.'
                    })
                    continue
                
                if role == 'sender':
                    active_transfers[otp]['sender_id'] = client_id
                    await websocket.send_json({
                        'type': 'otp_registered',
                        'message': 'You are now registered as sender. Waiting for receiver...'
                    })
                elif role == 'receiver':
                    active_transfers[otp]['receiver_id'] = client_id
                    await websocket.send_json({
                        'type': 'otp_registered',
                        'message': 'You are now registered as receiver. Connecting to sender...'
                    })
                    
                    # Notify sender that receiver has connected
                    sender_id = active_transfers[otp]['sender_id']
                    if sender_id:
                        await manager.send_personal_message({
                            'type': 'receiver_connected',
                            'message': 'Receiver has joined. Ready to transfer files.'
                        }, sender_id)
            
            elif message_type == 'file_metadata':
                otp = data.get('otp')
                if otp in active_transfers:
                    active_transfers[otp]['files'].append(data['metadata'])
                    
                    # Forward metadata to receiver if connected
                    receiver_id = active_transfers[otp]['receiver_id']
                    if receiver_id:
                        await manager.send_personal_message({
                            'type': 'file_incoming',
                            'metadata': data['metadata']
                        }, receiver_id)
            
            elif message_type == 'file_chunk':
                # Forward file chunk to the other party
                otp = data.get('otp')
                if otp in active_transfers:
                    sender_id = active_transfers[otp]['sender_id']
                    receiver_id = active_transfers[otp]['receiver_id']
                    
                    target_id = receiver_id if client_id == sender_id else sender_id
                    if target_id:
                        await manager.send_personal_message({
                            'type': 'file_chunk',
                            'chunk': data['chunk'],
                            'file_id': data['file_id'],
                            'is_last': data.get('is_last', False)
                        }, target_id)
            
            elif message_type == 'transfer_complete':
                otp = data.get('otp')
                if otp in active_transfers:
                    # Notify the other party
                    sender_id = active_transfers[otp]['sender_id']
                    receiver_id = active_transfers[otp]['receiver_id']
                    
                    target_id = receiver_id if client_id == sender_id else sender_id
                    if target_id:
                        await manager.send_personal_message({
                            'type': 'transfer_complete',
                            'message': 'File transfer completed successfully.'
                        }, target_id)
    
    except WebSocketDisconnect:
        manager.disconnect(client_id)
        # Clean up any transfers associated with this client
        for otp, data in active_transfers.items():
            if data['sender_id'] == client_id or data['receiver_id'] == client_id:
                # Notify the other party about disconnection
                other_id = data['receiver_id'] if data['sender_id'] == client_id else data['sender_id']
                if other_id:
                    asyncio.create_task(manager.send_personal_message({
                        'type': 'peer_disconnected',
                        'message': 'The other device disconnected.'
                    }, other_id))
                
                # Remove the transfer after a delay
                asyncio.create_task(remove_transfer_after_delay(otp))
    except Exception as e:
        logger.error(f"WebSocket error for client {client_id}: {e}")
        manager.disconnect(client_id)

async def remove_transfer_after_delay(otp, delay=10):
    """Remove a transfer after a delay"""
    await asyncio.sleep(delay)
    if otp in active_transfers:
        del active_transfers[otp]
        logger.info(f"Removed transfer session for OTP : {otp}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)