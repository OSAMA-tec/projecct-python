"""
CoAP Motion Sensor Bridge
========================

This module implements a CoAP server that bridges an Arduino-based motion sensor to web clients.
It provides real-time motion detection updates using CoAP (Constrained Application Protocol) 
and allows web clients to observe motion events through a WebSocket proxy.

Architecture Overview:
--------------------
                                                    
    +-------------+         +--------------+         +----------------+         +-------------+
    |   Arduino   | Serial  | CoAP Server  | CoAP   |   WebSocket   | WS      |    Web     |
    | Motion      |-------->| (This file)  |------->|    Proxy      |-------->|   Client   |
    | Sensor      |         |              |         |               |         |            |
    +-------------+         +--------------+         +----------------+         +-------------+

Components:
----------
1. MotionResource: CoAP resource handling motion sensor data
2. MotionSensorBridge: Manages serial connection and data publishing
3. Main Server: Runs the CoAP server and manages connections

Data Flow:
---------
1. Arduino sends motion data via Serial
2. MotionSensorBridge processes serial data
3. Data is published to CoAP observers
4. WebSocket proxy observes CoAP resource
5. Web clients receive real-time updates

Message Types:
------------
1. Sensor Reading:
   {
     "type": "sensor_reading",
     "value": 1/0,
     "status": "motion"/"no_motion"
   }

2. Motion Event:
   {
     "type": "event",
     "event": "motion_detected",
     "value": 1
   }

3. Status Update:
   {
     "type": "status",
     "status": "system_starting"/"sensor_ready"
   }

Configuration:
-------------
Environment variables (in .env file):
- COAP_HOST: CoAP server host (default: 127.0.0.1)
- COAP_PORT: CoAP server port (default: 5683)
- COAP_RESOURCE_PATH: Resource path (default: motion)
- SERIAL_PORT: Arduino serial port
- SERIAL_BAUD: Serial baud rate (default: 9600)

Usage Scenarios:
--------------
1. Motion Detection:
   - Arduino detects motion
   - Server receives serial data
   - Observers are notified
   - Web clients show real-time updates

2. System Status:
   - Server startup notification
   - Sensor ready confirmation
   - Connection status updates

3. Error Handling:
   - Serial connection issues
   - Server binding problems
   - Client disconnections

Dependencies:
------------
- aiocoap: CoAP protocol implementation
- pyserial: Serial communication
- python-dotenv: Environment configuration
"""

import serial
import json
import time
from datetime import datetime
import logging
from typing import Dict, Any
import sys
import os
from dotenv import load_dotenv
import asyncio
import threading
from queue import Queue
import socket
import aiocoap
import aiocoap.resource as resource
from aiocoap.numbers.codes import Code
from aiocoap import Message
from aiocoap.protocol import Context

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('motion_sensor.log'),
        logging.StreamHandler(sys.stdout)
    ]
)

# CoAP Configuration
COAP_HOST = os.getenv('COAP_HOST', '127.0.0.1')
COAP_PORT = int(os.getenv('COAP_PORT', 5683))
COAP_RESOURCE_PATH = os.getenv('COAP_RESOURCE_PATH', 'motion')

# Serial Configuration
SERIAL_PORT = os.getenv('SERIAL_PORT', '/dev/cu.usbserial-1430')
SERIAL_BAUD = 9600

class MotionResource(resource.Resource):
    """
    CoAP resource for handling motion sensor data.
    
    This resource implements the CoAP observe pattern, allowing clients to:
    1. GET current motion status
    2. Observe motion events in real-time
    3. Receive updates when motion is detected
    
    Attributes:
        content (bytes): Current sensor state as JSON bytes
        observers (set): Set of CoAP observers
        notify_queue (Queue): Queue for handling notifications
    """
    
    def __init__(self):
        super().__init__()
        self.content = b'{"type":"status","status":"initializing"}'
        self.observers = set()
        self.notify_queue = Queue()

    def notify_observers(self, data: bytes):
        """
        Notify all observers of new sensor data.
        
        Args:
            data (bytes): JSON-encoded sensor data to send to observers
        """
        self.content = data
        for observer in self.observers:
            observer.trigger()

    async def render_get(self, request):
        """
        Handle GET requests for current sensor state.
        
        Returns:
            Message: CoAP message containing current sensor state
        """
        return Message(payload=self.content, code=Code.CONTENT)

    async def render_post(self, request):
        """Handle POST requests"""
        self.content = request.payload
        self.notify_observers(self.content)
        return Message(code=Code.CHANGED, payload=self.content)

    def add_observer(self, observer):
        self.observers.add(observer)
        logging.info("Added new observer")

    def remove_observer(self, observer):
        self.observers.remove(observer)
        logging.info("Removed observer")

class MotionSensorBridge:
    """
    Bridge between Arduino motion sensor and CoAP server.
    
    Handles:
    1. Serial communication with Arduino
    2. Data processing and formatting
    3. Publishing updates to CoAP resource
    
    Attributes:
        serial_conn (serial.Serial): Serial connection to Arduino
        motion_resource (MotionResource): CoAP resource for motion data
    """
    
    def __init__(self):
        """Initialize the bridge with serial connection and motion resource."""
        self.serial_conn = None
        self.motion_resource = MotionResource()
        self.setup_serial()

    def setup_serial(self) -> None:
        """Setup serial connection with Arduino"""
        try:
            self.serial_conn = serial.Serial(
                port=SERIAL_PORT,
                baudrate=SERIAL_BAUD,
                timeout=1
            )
            time.sleep(2)  # Allow Arduino to reset
            logging.info(f"Serial connection established on {SERIAL_PORT}")
        except Exception as e:
            logging.error(f"Serial setup failed: {e}")
            raise

    def process_serial_data(self, line: str) -> None:
        """
        Process incoming serial data from Arduino.
        
        Handles different types of messages:
        1. Sensor readings (0/1)
        2. Motion detection events
        3. System status updates
        
        Args:
            line (str): Raw serial data line from Arduino
        """
        try:
            # Parse the text data
            data = {}
            line = line.strip()
            
            if "Sensor Reading:" in line:
                value = line.split(":")[-1].strip()
                data = {
                    "type": "sensor_reading",
                    "value": int(value),
                    "status": "motion" if int(value) == 1 else "no_motion"
                }
            elif "Motion Detected!" in line:
                data = {
                    "type": "event",
                    "event": "motion_detected",
                    "value": 1
                }
            elif "System Starting..." in line:
                data = {
                    "type": "status",
                    "status": "system_starting"
                }
            elif "Sensor Ready!" in line:
                data = {
                    "type": "status",
                    "status": "sensor_ready"
                }
            
            # Only publish if we have valid data
            if data:
                self.publish_data(data)
            
        except Exception as e:
            logging.error(f"Error processing data: {e}, Data: {line}")

    def publish_data(self, data: Dict[str, Any]) -> None:
        """Publish data to CoAP observers"""
        try:
            # Add timestamp and format data
            payload = {
                **data,
                'timestamp': datetime.now().isoformat(),
                'device_id': socket.gethostname()
            }
            
            # Convert to JSON and notify observers
            json_data = json.dumps(payload).encode('utf-8')
            self.motion_resource.notify_observers(json_data)
            logging.info(f"Published: {payload}")
            
        except Exception as e:
            logging.error(f"Error publishing data: {e}")

    def run_serial_reader(self) -> None:
        """Main loop to read from serial"""
        logging.info("Starting serial reader...")
        
        try:
            while True:
                if self.serial_conn.in_waiting:
                    line = self.serial_conn.readline().decode('utf-8').strip()
                    if line:
                        self.process_serial_data(line)
                time.sleep(0.1)  # Prevent CPU overload
                
        except KeyboardInterrupt:
            logging.info("Shutting down serial reader...")
        except Exception as e:
            logging.error(f"Runtime error in serial reader: {e}")
        finally:
            self.cleanup()

    def cleanup(self) -> None:
        """Cleanup resources"""
        try:
            if self.serial_conn:
                self.serial_conn.close()
            logging.info("Cleanup completed")
        except Exception as e:
            logging.error(f"Error during cleanup: {e}")

async def create_server_context(root_resource):
    """Create a CoAP server context with proper binding"""
    # Create the site with our resources
    site = resource.Site()
    site.add_resource([COAP_RESOURCE_PATH], root_resource)
    
    # Create server context with bind info
    context = await Context.create_server_context(
        site,
        bind=(COAP_HOST, COAP_PORT)
    )
    
    return context

async def main():
    try:
        # Create motion sensor bridge
        bridge = MotionSensorBridge()
        
        # Create server context with motion resource
        context = await create_server_context(bridge.motion_resource)
        logging.info(f"CoAP server started on {COAP_HOST}:{COAP_PORT}")
        
        # Start serial reader in a separate thread
        serial_thread = threading.Thread(target=bridge.run_serial_reader)
        serial_thread.daemon = True
        serial_thread.start()
        
        # Keep the server running
        while True:
            await asyncio.sleep(1)
            
    except Exception as e:
        logging.error(f"Server error: {e}")
        logging.exception("Detailed error:")
    except KeyboardInterrupt:
        logging.info("Shutting down...")
    finally:
        if 'context' in locals():
            await context.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Program terminated by user") 