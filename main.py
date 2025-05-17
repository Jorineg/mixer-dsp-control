from flask import Flask, render_template, request, redirect, url_for, jsonify
import threading
import json
import time
import socket
import os
import subprocess
import platform
import paho.mqtt.client as mqtt
from datetime import datetime

app = Flask(__name__)


# Data Structures
class Settings:
    def __init__(self):
        self.dsp_ip = "192.168.1.101"
        self.mqtt_broker = "192.168.1.102"
        self.mqtt_port = 1883
        self.mqtt_username = ""
        self.mqtt_password = ""
        self.mqtt_topics = ["dsp/elki", "dsp/konferenz"]
        self.main_on = True
        self.flash_indication = True
        self.dsp_config = []  # List of dicts with DSP channel configurations
        self.load_settings()

    def load_settings(self):
        try:
            with open("settings.json", "r") as f:
                data = json.load(f)
                self.dsp_ip = data.get("dsp_ip", self.dsp_ip)
                self.mqtt_broker = data.get("mqtt_broker", self.mqtt_broker)
                self.mqtt_port = data.get("mqtt_port", self.mqtt_port)
                self.mqtt_username = data.get("mqtt_username", self.mqtt_username)
                self.mqtt_password = data.get("mqtt_password", self.mqtt_password)
                self.mqtt_topics = data.get("mqtt_topics", self.mqtt_topics)
                self.main_on = data.get("main_on", self.main_on)
                self.flash_indication = data.get(
                    "flash_indication", self.flash_indication
                )
                self.dsp_config = data.get("dsp_config", [])
        except FileNotFoundError:
            self.save_settings()

    def save_settings(self):
        data = {
            "dsp_ip": self.dsp_ip,
            "mqtt_broker": self.mqtt_broker,
            "mqtt_port": self.mqtt_port,
            "mqtt_username": self.mqtt_username,
            "mqtt_password": self.mqtt_password,
            "mqtt_topics": self.mqtt_topics,
            "main_on": self.main_on,
            "flash_indication": self.flash_indication,
            "dsp_config": self.dsp_config,
        }
        with open("settings.json", "w") as f:
            json.dump(data, f, indent=4)

        # Synchronize internal state with the current DSP config
        self.sync_internal_state_with_config()

    def sync_internal_state_with_config(self):
        """Synchronize internal state with the current DSP configuration."""
        # Get all configured channel names
        configured_channels = set(item["channel_name"] for item in self.dsp_config)

        # Get all channels in internal state
        state_channels = set(internal_state.dsp_channel_states.keys())

        # Add missing channels to internal state
        for channel_name in configured_channels:
            if channel_name not in internal_state.dsp_channel_states:
                internal_state.dsp_channel_states[channel_name] = {
                    "level_value": 0,  # Default to 0 volume
                    "mute_state": "off",  # Default to unmuted
                }

        # Remove channels from internal state that are no longer in the config
        channels_to_remove = state_channels - configured_channels
        for channel_name in channels_to_remove:
            if channel_name in internal_state.dsp_channel_states:
                del internal_state.dsp_channel_states[channel_name]

        # Schedule a save of the internal state if any changes were made
        if configured_channels != state_channels:
            internal_state.schedule_save()


class InternalState:
    def __init__(self):
        self.dsp_channel_states = (
            {}
        )  # {channel_name: {'level_value': None, 'mute_state': None}}
        self.last_mqtt_message = None
        self.last_mqtt_message_time = None
        self.last_dsp_message_time = None
        self.internal_state_changed = False
        self.load_state()

    def load_state(self):
        try:
            with open("internal_state.json", "r") as f:
                data = json.load(f)
                self.dsp_channel_states = data.get("dsp_channel_states", {})
        except FileNotFoundError:
            self.save_state()

    def save_state(self):
        data = {
            "dsp_channel_states": self.dsp_channel_states,
        }
        with open("internal_state.json", "w") as f:
            json.dump(data, f, indent=4)

    def schedule_save(self):
        self.internal_state_changed = True


# Global instances
settings = Settings()
internal_state = InternalState()
save_lock = threading.Lock()


# Function to ping an IP address
def ping_ip(ip_address):
    # Determine the operating system
    operating_system = platform.system().lower()

    try:
        if operating_system == "windows":
            # Windows ping command
            output = subprocess.check_output(
                ["ping", "-n", "1", "-w", "1000", ip_address],
                stderr=subprocess.STDOUT,
                universal_newlines=True,
            )
        else:
            # Linux/Unix ping command
            output = subprocess.check_output(
                ["ping", "-c", "1", "-W", "1", ip_address],
                stderr=subprocess.STDOUT,
                universal_newlines=True,
            )
        return True
    except subprocess.CalledProcessError:
        return False


# Network Communication Threads
class MQTTCommunicator(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
        self.mqtt_connected = False
        self.mqtt_client = None
        self.running = True

    def run(self):
        while self.running:
            if settings.mqtt_broker and settings.main_on:
                self.connect_to_mqtt()
                if self.mqtt_connected:
                    time.sleep(5)  # Check connection periodically
                else:
                    time.sleep(5)  # Retry every 5 seconds
            else:
                time.sleep(5)

    def connect_to_mqtt(self):
        try:
            if self.mqtt_client is None:
                self.mqtt_client = mqtt.Client(client_id="dsp_controller")

                # Set up callbacks
                self.mqtt_client.on_connect = self.on_connect
                self.mqtt_client.on_message = self.on_message
                self.mqtt_client.on_disconnect = self.on_disconnect

                # Set last will for connection status monitoring
                self.mqtt_client.will_set("dsp/status", "offline", 0, True)

                # Set credentials if provided
                if settings.mqtt_username and settings.mqtt_password:
                    self.mqtt_client.username_pw_set(
                        settings.mqtt_username, settings.mqtt_password
                    )

                # Connect with keepalive of 60 seconds
                self.mqtt_client.connect(settings.mqtt_broker, settings.mqtt_port, 60)
                self.mqtt_client.loop_start()

            print("Connecting to MQTT broker...")

        except Exception as e:
            self.mqtt_connected = False
            print(f"Failed to connect to MQTT broker: {e}")
            time.sleep(5)

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.mqtt_connected = True
            print(f"Connected to MQTT broker, rc: {rc}")

            # Publish online status
            client.publish("dsp/status", "online", 0, True)

            # Subscribe to all configured topics
            for topic in settings.mqtt_topics:
                client.subscribe(f"{topic}/volume")
                client.subscribe(f"{topic}/mute")
            print(f"Subscribed to MQTT topics: {settings.mqtt_topics}")
        else:
            self.mqtt_connected = False
            print(f"Failed to connect to MQTT broker, rc: {rc}")

    def on_disconnect(self, client, userdata, rc):
        self.mqtt_connected = False
        print(f"Disconnected from MQTT broker, rc: {rc}")

    def on_message(self, client, userdata, msg):
        # Process received MQTT message
        print(f"Received MQTT message: {msg.topic} = {msg.payload.decode()}")

        try:
            # Extract channel and command type from topic
            topic_parts = msg.topic.split("/")
            if len(topic_parts) >= 3:
                channel_name = topic_parts[1]  # e.g., "elki" from "dsp/elki/volume"
                command_type = topic_parts[2]  # e.g., "volume" or "mute"

                # Process the message based on command type
                if command_type == "volume":
                    # Convert payload to level value (0-127)
                    try:
                        level_value = int(float(msg.payload.decode()))
                        # Ensure value is within range
                        level_value = max(0, min(level_value, 127))
                        self.handle_level_change(channel_name, level_value)
                    except ValueError:
                        print(f"Invalid volume value: {msg.payload.decode()}")

                elif command_type == "mute":
                    # Convert payload to mute state ("on" or "off")
                    payload = msg.payload.decode().lower()
                    if payload in ["on", "true", "1"]:
                        mute_state = "on"
                    elif payload in ["off", "false", "0"]:
                        mute_state = "off"
                    else:
                        print(f"Invalid mute value: {payload}")
                        return

                    self.handle_mute_change(channel_name, mute_state)

            # Update last MQTT message info
            internal_state.last_mqtt_message = f"{msg.topic}: {msg.payload.decode()}"
            internal_state.last_mqtt_message_time = time.time()

        except Exception as e:
            print(f"Error processing MQTT message: {e}")

    def handle_level_change(self, channel_name, level_value):
        if channel_name not in internal_state.dsp_channel_states:
            internal_state.dsp_channel_states[channel_name] = {
                "level_value": None,
                "mute_state": None,
            }

        internal_state.dsp_channel_states[channel_name]["level_value"] = level_value
        internal_state.schedule_save()

        # Send to DSP if connected
        if dsp_communicator.dsp_connected:
            dsp_communicator.send_dsp_message(channel_name, "level", level_value)

    def handle_mute_change(self, channel_name, mute_state):
        if channel_name not in internal_state.dsp_channel_states:
            internal_state.dsp_channel_states[channel_name] = {
                "level_value": None,
                "mute_state": None,
            }

        internal_state.dsp_channel_states[channel_name]["mute_state"] = mute_state
        internal_state.schedule_save()

        # Send to DSP if connected
        if dsp_communicator.dsp_connected:
            dsp_communicator.send_dsp_message(channel_name, "mute", mute_state)

    def publish_dsp_state(self, channel_name, state_type, value):
        """Publish DSP state changes back to MQTT for status reporting"""
        if not self.mqtt_connected:
            return

        try:
            topic = f"dsp/{channel_name}/{state_type}"
            if state_type == "level":
                self.mqtt_client.publish(topic, str(value), qos=0, retain=True)
            elif state_type == "mute":
                self.mqtt_client.publish(topic, value, qos=0, retain=True)
        except Exception as e:
            print(f"Failed to publish to MQTT: {e}")

    def stop(self):
        self.running = False
        if self.mqtt_client:
            self.mqtt_client.publish("dsp/status", "offline", 0, True)
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()


class DSPCommunicator(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
        self.dsp_connected = False
        self.dsp_socket = None
        self.running = True
        self.last_flash_time = 0

    def run(self):
        while self.running:
            if settings.dsp_ip and settings.main_on:
                if ping_ip(settings.dsp_ip):
                    if not self.dsp_connected:
                        self.connect_to_dsp()
                        if self.dsp_connected:
                            self.send_internal_state_to_dsp()
                    # Keep checking every 5 seconds
                    time.sleep(5)
                else:
                    if self.dsp_connected:
                        self.dsp_connected = False
                        print(f"DSP {settings.dsp_ip} is not reachable.")
                    time.sleep(5)
            else:
                time.sleep(5)

    def connect_to_dsp(self):
        try:
            if self.dsp_socket is None:
                self.dsp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.dsp_socket.bind(("", 49152))  # Bind to an available port
                self.dsp_socket.settimeout(1.0)  # Set timeout for recvfrom
                # Start a thread to receive messages
                threading.Thread(target=self.receive_dsp_messages, daemon=True).start()
            self.dsp_connected = True
            print("Connected to DSP")
        except Exception as e:
            self.dsp_connected = False
            print(f"Failed to connect to DSP: {e}")
            time.sleep(5)

    def send_internal_state_to_dsp(self):
        for dsp_channel_name, state in internal_state.dsp_channel_states.items():
            if state["level_value"] is not None:
                self.send_dsp_message(dsp_channel_name, "level", state["level_value"])
            if state["mute_state"] is not None:
                self.send_dsp_message(dsp_channel_name, "mute", state["mute_state"])

    def receive_dsp_messages(self):
        try:
            while self.dsp_connected and self.running:
                try:
                    data, addr = self.dsp_socket.recvfrom(1024)
                    if data:
                        # Process DSP message
                        self.process_dsp_message(data)
                        internal_state.last_dsp_message_time = time.time()
                    else:
                        pass  # No data received
                except socket.timeout:
                    pass  # Timeout occurred, no data received
        except Exception as e:
            self.dsp_connected = False
            print(f"DSP connection error: {e}")

    def process_dsp_message(self, data):
        # Parse the data according to DSP protocol
        # Expected message length is 20 bytes
        print("got dsp message")
        if len(data) == 20:
            print("20 bytes")
            # Extract XX, YY, ZZ from the message
            XX = data[15]
            YY = data[16]
            ZZ = data[17]
            # Find corresponding DSP channel name based on XX
            dsp_channel_name = None
            message_type = None
            for channel in settings.dsp_config:
                if XX == int(channel["level_address"], 16):
                    dsp_channel_name = channel["channel_name"]
                    message_type = "level"
                    break
                elif XX == int(channel["mute_address"], 16):
                    dsp_channel_name = channel["channel_name"]
                    message_type = "mute"
                    break

            if dsp_channel_name:
                if dsp_channel_name not in internal_state.dsp_channel_states:
                    internal_state.dsp_channel_states[dsp_channel_name] = {
                        "level_value": None,
                        "mute_state": None,
                    }

                if message_type == "level":
                    level_value = (YY << 8) + ZZ
                    previous_value = internal_state.dsp_channel_states[
                        dsp_channel_name
                    ]["level_value"]
                    internal_state.dsp_channel_states[dsp_channel_name][
                        "level_value"
                    ] = level_value
                    internal_state.schedule_save()

                    # Publish state change to MQTT if connected
                    if mqtt_communicator.mqtt_connected:
                        mqtt_communicator.publish_dsp_state(
                            dsp_channel_name, "level", level_value
                        )

                elif message_type == "mute":
                    mute_state = "on" if (YY, ZZ) == (0xFF, 0xFF) else "off"
                    previous_state = internal_state.dsp_channel_states[
                        dsp_channel_name
                    ]["mute_state"]
                    internal_state.dsp_channel_states[dsp_channel_name][
                        "mute_state"
                    ] = mute_state
                    internal_state.schedule_save()

                    # Publish state change to MQTT if connected
                    if mqtt_communicator.mqtt_connected:
                        mqtt_communicator.publish_dsp_state(
                            dsp_channel_name, "mute", mute_state
                        )

        else:
            print(f"Received unexpected DSP message length: {len(data)}")

    def send_dsp_message(self, dsp_channel_name, message_type, value):
        # Build the DSP message according to the protocol
        # 00 00 00 02 00 00 00 14 00 00 00 01 03 00 00 XX YY ZZ 00 00
        header = bytes(
            [
                0x00,
                0x00,
                0x00,
                0x02,
                0x00,
                0x00,
                0x00,
                0x14,
                0x00,
                0x00,
                0x00,
                0x01,
                0x03,
                0x00,
                0x00,
            ]
        )
        # Find addresses from dsp_config
        XX = None
        YY = 0x00
        ZZ = 0x00

        for channel in settings.dsp_config:
            if channel["channel_name"] == dsp_channel_name:
                if message_type == "level":
                    value *= 2  # multiply by to go from 0-127 to 0-255
                    XX = int(channel["level_address"], 16)
                    ZZ = (value >> 8) & 0xFF  # swapped YY and ZZ
                    YY = value & 0xFF
                elif message_type == "mute":
                    XX = int(channel["mute_address"], 16)
                    if value == "on":
                        YY, ZZ = 0xFF, 0xFF
                    elif value == "off":
                        YY, ZZ = 0x00, 0x00
                break

        if XX is not None:
            payload = bytes([XX, YY, ZZ, 0x00, 0x00])
            message = header + payload
            # Send message to DSP
            try:
                self.dsp_socket.sendto(message, (settings.dsp_ip, 49184))
                print(
                    f"send {message_type} message to dsp: {message.hex()}, value {value}"
                )
            except Exception as e:
                print(f"Failed to send DSP message: {e}")

    def stop(self):
        self.running = False
        if self.dsp_socket:
            self.dsp_socket.close()


# Instantiate communicators
mqtt_communicator = MQTTCommunicator()
dsp_communicator = DSPCommunicator()
mqtt_communicator.start()
dsp_communicator.start()


# Functions to save internal state periodically
def save_internal_state_periodically():
    while True:
        time.sleep(1)
        if internal_state.internal_state_changed:
            with save_lock:
                time.sleep(5)  # Wait for 5 seconds of no changes
                if internal_state.internal_state_changed:
                    internal_state.save_state()
                    internal_state.internal_state_changed = False


state_saver_thread = threading.Thread(target=save_internal_state_periodically)
state_saver_thread.start()


# Flask Routes
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        # Handle form submission
        settings.dsp_ip = request.form.get("dsp_ip", settings.dsp_ip)
        settings.mqtt_broker = request.form.get("mqtt_broker", settings.mqtt_broker)
        settings.mqtt_port = int(request.form.get("mqtt_port", settings.mqtt_port))
        settings.mqtt_username = request.form.get(
            "mqtt_username", settings.mqtt_username
        )
        settings.mqtt_password = request.form.get(
            "mqtt_password", settings.mqtt_password
        )
        settings.main_on = "main_on" in request.form
        settings.flash_indication = "flash_indication" in request.form

        # Handle MQTT topics
        settings.mqtt_topics = json.loads(request.form.get("mqtt_topics", "[]"))

        # Handle DSP config table
        settings.dsp_config = json.loads(request.form.get("dsp_config", "[]"))

        settings.save_settings()

        # Restart MQTT connection with new settings
        mqtt_communicator.mqtt_connected = False
        if mqtt_communicator.mqtt_client:
            mqtt_communicator.mqtt_client.loop_stop()
            mqtt_communicator.mqtt_client.disconnect()
        mqtt_communicator.mqtt_client = None

        return redirect(url_for("index"))
    else:
        # Render the index page with current settings and state
        return render_template(
            "index.html",
            settings=settings,
            internal_state=internal_state,
            mqtt_status=get_mqtt_status(),
            dsp_status=get_dsp_status(),
        )


@app.route("/restart_device", methods=["POST"])
def restart_device():
    # Restart the Raspberry Pi Zero
    os.system("sudo reboot")
    return "Restarting device...", 200


@app.route("/api/state")
def api_state():
    # Return the internal state and last MQTT message as JSON
    last_mqtt_message_time = internal_state.last_mqtt_message_time
    if last_mqtt_message_time:
        time_since_last_mqtt = time.time() - last_mqtt_message_time
    else:
        time_since_last_mqtt = None
    return jsonify(
        {
            "dsp_channel_states": internal_state.dsp_channel_states,
            "last_mqtt_message": internal_state.last_mqtt_message,
            "time_since_last_mqtt": time_since_last_mqtt,
            "mqtt_status": get_mqtt_status(),
            "dsp_status": get_dsp_status(),
        }
    )


@app.route("/api/update_channel", methods=["POST"])
def update_channel():
    """Handle channel updates from the web interface"""
    try:
        channel_name = request.form.get("channel_name")
        control_type = request.form.get("control_type")
        value = request.form.get("value")

        if not channel_name or not control_type or value is None:
            return (
                jsonify({"status": "error", "message": "Missing required parameters"}),
                400,
            )

        # Initialize channel state if it doesn't exist
        if channel_name not in internal_state.dsp_channel_states:
            internal_state.dsp_channel_states[channel_name] = {
                "level_value": None,
                "mute_state": None,
            }

        # Update internal state based on control type
        if control_type == "volume":
            try:
                level_value = int(float(value))
                # Ensure value is within range
                level_value = max(0, min(level_value, 127))
                internal_state.dsp_channel_states[channel_name][
                    "level_value"
                ] = level_value
                internal_state.schedule_save()

                # Send to DSP if connected
                if dsp_communicator.dsp_connected:
                    dsp_communicator.send_dsp_message(
                        channel_name, "level", level_value
                    )

                # Publish to MQTT if connected
                if mqtt_communicator.mqtt_connected:
                    mqtt_communicator.publish_dsp_state(
                        channel_name, "level", level_value
                    )

                return jsonify({"status": "success"})
            except ValueError:
                return (
                    jsonify({"status": "error", "message": "Invalid volume value"}),
                    400,
                )

        elif control_type == "mute":
            if value in ["on", "off"]:
                internal_state.dsp_channel_states[channel_name]["mute_state"] = value
                internal_state.schedule_save()

                # Send to DSP if connected
                if dsp_communicator.dsp_connected:
                    dsp_communicator.send_dsp_message(channel_name, "mute", value)

                # Publish to MQTT if connected
                if mqtt_communicator.mqtt_connected:
                    mqtt_communicator.publish_dsp_state(channel_name, "mute", value)

                return jsonify({"status": "success"})
            else:
                return (
                    jsonify({"status": "error", "message": "Invalid mute value"}),
                    400,
                )

        else:
            return jsonify({"status": "error", "message": "Invalid control type"}), 400

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


def get_mqtt_status():
    if mqtt_communicator.mqtt_connected:
        if (
            internal_state.last_mqtt_message_time
            and time.time() - internal_state.last_mqtt_message_time < 60
        ):
            return "connected (receiving messages)"
        else:
            return "connected (no data)"
    else:
        return "offline"


def get_dsp_status():
    if dsp_communicator.dsp_connected:
        return "connected"
    else:
        return "offline"


# Run Flask app
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
