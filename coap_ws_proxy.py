import asyncio
import websockets
import json
import logging
from aiocoap import *
from dotenv import load_dotenv
import os

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Configuration
WS_HOST = '127.0.0.1'
WS_PORT = 8080
COAP_SERVER = 'coap://127.0.0.1:5683'

class CoAPWebSocketProxy:
    def __init__(self):
        self.clients = set()
        self.protocol = None
        
    async def setup_coap(self):
        """Setup CoAP client protocol"""
        self.protocol = await Context.create_client_context()
        
    async def observe_coap_resource(self, resource_path):
        """Observe CoAP resource and forward updates to WebSocket clients"""
        request = Message(code=GET, uri=f'{COAP_SERVER}/{resource_path}', observe=0)
        
        try:
            async for response in self.protocol.request(request).observation():
                if response.code.is_successful():
                    # Forward to all connected WebSocket clients
                    message = response.payload.decode('utf-8')
                    await self.broadcast(message)
                else:
                    logging.error(f"Error observing resource: {response.code}")
        except Exception as e:
            logging.error(f"CoAP observation error: {e}")
            
    async def handle_websocket(self, websocket, path):
        """Handle WebSocket connections"""
        try:
            self.clients.add(websocket)
            logging.info(f"New WebSocket client connected. Total clients: {len(self.clients)}")
            
            async for message in websocket:
                try:
                    data = json.loads(message)
                    if data.get('type') == 'subscribe':
                        # Start observing the CoAP resource
                        asyncio.create_task(
                            self.observe_coap_resource(data.get('resource', 'motion'))
                        )
                except json.JSONDecodeError:
                    logging.error("Invalid JSON message received")
                    
        except websockets.exceptions.ConnectionClosed:
            logging.info("WebSocket connection closed")
        finally:
            self.clients.remove(websocket)
            
    async def broadcast(self, message):
        """Broadcast message to all connected WebSocket clients"""
        if not self.clients:
            return
            
        disconnected = set()
        for client in self.clients:
            try:
                await client.send(message)
            except websockets.exceptions.ConnectionClosed:
                disconnected.add(client)
                
        # Remove disconnected clients
        self.clients -= disconnected

async def main():
    # Create proxy instance
    proxy = CoAPWebSocketProxy()
    
    # Setup CoAP client
    await proxy.setup_coap()
    
    # Start WebSocket server
    async with websockets.serve(proxy.handle_websocket, WS_HOST, WS_PORT):
        logging.info(f"WebSocket proxy started on ws://{WS_HOST}:{WS_PORT}")
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Proxy server terminated by user") 