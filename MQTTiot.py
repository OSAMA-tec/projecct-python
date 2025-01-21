import serial
import json
import time
from datetime import datetime
import paho.mqtt.client as mqtt
import logging
from typing import Dict, Any
import sys
import os
from dotenv import load_dotenv
import ssl
import certifi
import subprocess
import signal

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

# MQTT Configuration from .env
MQTT_BROKER = os.getenv('MQTT_BROKER')
MQTT_PORT = int(os.getenv('MQTT_PORT', 8883))
MQTT_TOPIC = os.getenv('MQTT_TOPIC', 'securespace/motion')
MQTT_CLIENT_ID = os.getenv('MQTT_CLIENT_ID', 'securespace_bridge')
MQTT_USERNAME = os.getenv('MQTT_USERNAME')
MQTT_PASSWORD = os.getenv('MQTT_PASSWORD')

# Serial Configuration
SERIAL_PORT = os.getenv('SERIAL_PORT', '/dev/cu.usbserial-1430')
SERIAL_BAUD = 9600
MAX_SERIAL_RETRIES = 3
RETRY_DELAY = 2  # seconds

class MotionSensorBridge:
    def __init__(self):
        self.serial_conn = None
        self.mqtt_client = None
        self.connected = False
        self.setup_mqtt()
        self.setup_serial_with_retry()

    def force_release_port(self) -> None:
        """Force release the serial port by killing any process using it"""
        try:
            # Find process using the port
            cmd = f"lsof | grep {SERIAL_PORT.split('/')[-1]}"
            result = subprocess.run(cmd, shell=True, text=True, capture_output=True)
            
            if result.stdout:
                # Extract PID and kill the process
                pid = result.stdout.split()[1]
                subprocess.run(['kill', pid], check=True)
                logging.info(f"Killed process {pid} using the serial port")
                time.sleep(1)  # Wait for port to be released
        except Exception as e:
            logging.warning(f"Failed to force release port: {e}")

    def setup_serial_with_retry(self) -> None:
        """Setup serial connection with retries and force release"""
        retries = 0
        while retries < MAX_SERIAL_RETRIES:
            try:
                # Close port if it was previously open
                if self.serial_conn:
                    self.serial_conn.close()
                    time.sleep(1)
                
                # Try to force release the port if it's busy
                self.force_release_port()
                
                # Try to open the port
                self.setup_serial()
                logging.info("Serial connection established successfully")
                return
            except serial.SerialException as e:
                retries += 1
                logging.warning(f"Serial connection attempt {retries} failed: {e}")
                if retries < MAX_SERIAL_RETRIES:
                    logging.info(f"Retrying in {RETRY_DELAY} seconds...")
                    time.sleep(RETRY_DELAY)
                else:
                    logging.error("Max retries reached. Could not establish serial connection")
                    raise

    def setup_mqtt(self) -> None:
        """Setup MQTT client with callbacks and TLS"""
        try:
            self.mqtt_client = mqtt.Client(MQTT_CLIENT_ID, protocol=mqtt.MQTTv5)
            
            # Enable TLS/SSL with proper certificate path
            self.mqtt_client.tls_set(
                ca_certs=certifi.where(),
                tls_version=ssl.PROTOCOL_TLS,
                cert_reqs=ssl.CERT_REQUIRED
            )
            self.mqtt_client.tls_insecure_set(False)
            
            # Set callbacks
            self.mqtt_client.on_connect = self.on_mqtt_connect
            self.mqtt_client.on_disconnect = self.on_mqtt_disconnect
            
            # Set credentials
            if MQTT_USERNAME and MQTT_PASSWORD:
                self.mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
            
            # Connect to broker
            logging.info(f"Connecting to MQTT broker: {MQTT_BROKER}:{MQTT_PORT}")
            self.mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
            self.mqtt_client.loop_start()
            logging.info("MQTT client setup completed")
        except Exception as e:
            logging.error(f"MQTT setup failed: {str(e)}")
            raise

    def setup_serial(self) -> None:
        """Setup serial connection with Arduino"""
        try:
            self.serial_conn = serial.Serial(
                port=SERIAL_PORT,
                baudrate=SERIAL_BAUD,
                timeout=1,
                exclusive=True  # Try to get exclusive access
            )
            time.sleep(2)  # Allow Arduino to reset
            logging.info(f"Serial connection established on {SERIAL_PORT}")
        except Exception as e:
            logging.error(f"Serial setup failed: {e}")
            raise

    def on_mqtt_connect(self, client, userdata, flags, rc, properties=None) -> None:
        """Callback for when MQTT client connects (MQTTv5)"""
        if rc == 0:
            self.connected = True
            logging.info("Connected to MQTT broker")
        else:
            logging.error(f"Failed to connect to MQTT broker with code: {rc}")

    def on_mqtt_disconnect(self, client, userdata, rc) -> None:
        """Callback for when MQTT client disconnects"""
        self.connected = False
        logging.warning(f"Disconnected from MQTT broker with code: {rc}")

    def process_serial_data(self, line: str) -> None:
        """Process data received from Arduino"""
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
            else:
                logging.debug(f"Ignored message: {line}")
                
        except Exception as e:
            logging.error(f"Error processing data: {e}, Data: {line}")

    def publish_data(self, data: Dict[str, Any]) -> None:
        """Publish data to MQTT broker"""
        try:
            # Add timestamp and format data
            payload = {
                **data,
                'timestamp': datetime.now().isoformat(),
                'device_id': MQTT_CLIENT_ID
            }
            
            # Publish to MQTT
            result = self.mqtt_client.publish(
                MQTT_TOPIC,
                json.dumps(payload),
                qos=1
            )
            
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                logging.info(f"Published: {payload}")
            else:
                logging.error(f"Failed to publish: {result.rc}")
        except Exception as e:
            logging.error(f"Error publishing data: {e}")

    def run(self) -> None:
        """Main loop to read from serial and publish to MQTT"""
        logging.info("Starting motion sensor bridge...")
        
        try:
            while True:
                if self.serial_conn.in_waiting:
                    line = self.serial_conn.readline().decode('utf-8').strip()
                    if line:
                        self.process_serial_data(line)
                        logging.debug(f"Received: {line}")
                time.sleep(0.1)  # Prevent CPU overload
                
        except KeyboardInterrupt:
            logging.info("Shutting down...")
        except Exception as e:
            logging.error(f"Runtime error: {e}")
        finally:
            self.cleanup()

    def cleanup(self) -> None:
        """Cleanup resources"""
        try:
            if self.serial_conn:
                self.serial_conn.close()
            if self.mqtt_client:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()
            logging.info("Cleanup completed")
        except Exception as e:
            logging.error(f"Error during cleanup: {e}")

def main():
    try:
        bridge = MotionSensorBridge()
        bridge.run()
    except Exception as e:
        logging.error(f"Failed to start bridge: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
