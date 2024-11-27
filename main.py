from flask import Flask, render_template, request, redirect, url_for, jsonify
import threading
import json
import time
import socket
import os
import subprocess
import platform
from datetime import datetime

app = Flask(__name__)


# Data Structures
class Settings:
    def __init__(self):
        self.mixer_ip = "192.168.1.100"
        self.dsp_ip = "192.168.1.101"
        self.main_on = True
        self.bidirectional = True
        self.flash_indication = True
        self.midi_channel_number = 0  # MIDI channel N (0-15)
        self.mixer_config = []  # List of dicts with mixer channel mappings
        self.dsp_config = []  # List of dicts with DSP channel configurations
        self.load_settings()

    def load_settings(self):
        try:
            with open("settings.json", "r") as f:
                data = json.load(f)
                self.mixer_ip = data.get("mixer_ip", self.mixer_ip)
                self.dsp_ip = data.get("dsp_ip", self.dsp_ip)
                self.main_on = data.get("main_on", self.main_on)
                self.bidirectional = data.get("bidirectional", self.bidirectional)
                self.flash_indication = data.get(
                    "flash_indication", self.flash_indication
                )
                self.midi_channel_number = data.get(
                    "midi_channel_number", self.midi_channel_number
                )
                self.mixer_config = data.get("mixer_config", [])
                self.dsp_config = data.get("dsp_config", [])
        except FileNotFoundError:
            self.save_settings()

    def save_settings(self):
        data = {
            "mixer_ip": self.mixer_ip,
            "dsp_ip": self.dsp_ip,
            "main_on": self.main_on,
            "bidirectional": self.bidirectional,
            "flash_indication": self.flash_indication,
            "midi_channel_number": self.midi_channel_number,
            "mixer_config": self.mixer_config,
            "dsp_config": self.dsp_config,
        }
        with open("settings.json", "w") as f:
            json.dump(data, f, indent=4)


class InternalState:
    def __init__(self):
        self.dsp_channel_states = (
            {}
        )  # {channel_name: {'level_value': None, 'mute_state': None}}
        self.last_midi_message = None
        self.last_midi_message_time = None
        self.last_dsp_message_time = None  # Track last DSP message time
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
class MixerCommunicator(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
        self.mixer_connected = False
        self.mixer_socket = None
        self.running = True

    def run(self):
        while self.running:
            if settings.mixer_ip and settings.main_on:
                self.connect_to_mixer()
                if self.mixer_connected:
                    self.receive_midi_messages()
                else:
                    time.sleep(5)
            else:
                time.sleep(5)

    def connect_to_mixer(self):
        try:
            self.mixer_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.mixer_socket.connect((settings.mixer_ip, 51325))
            self.mixer_connected = True
            # Initiate MIDI transfer by sending a MIDI "note on" to mixer
            self.send_midi_message(
                bytes([0x90 | settings.midi_channel_number, 0x00, 0x7F])
            )  # Example note on message
            print("Connected to Mixer")
            # If bidirectional, send internal state to mixer
            if settings.bidirectional:
                self.send_internal_state_to_mixer()
        except Exception as e:
            self.mixer_connected = False
            print(f"Failed to connect to Mixer: {e}")
            time.sleep(5)

    def send_internal_state_to_mixer(self):
        # Send the entire internal state to the mixer as MIDI messages
        for mapping in settings.mixer_config:
            dsp_channel_name = mapping["dsp_channel_name"]
            state = internal_state.dsp_channel_states.get(
                dsp_channel_name, {"level_value": None, "mute_state": None}
            )
            if state["level_value"] is not None:
                self.send_level_to_mixer(mapping, state["level_value"])
            if state["mute_state"] is not None:
                self.send_mute_to_mixer(mapping, state["mute_state"])

    def receive_midi_messages(self):
        try:
            buffer = b""
            while self.mixer_connected and self.running:
                data = self.mixer_socket.recv(1024)
                if data:
                    buffer += data
                    while len(buffer) >= 12:  # Each expected message is 12 bytes long
                        message = buffer[:12]
                        buffer = buffer[12:]
                        print(f"Got MIDI message: {message.hex()}")
                        self.process_midi_message(message)
                        print("Processed MIDI message")
                else:
                    self.mixer_connected = False
                    self.mixer_socket.close()
                    print("Mixer connection closed")
                    break
        except Exception as e:
            self.mixer_connected = False
            self.mixer_socket.close()
            print(f"Mixer connection error: {e}")

    def process_midi_message(self, data):
        midi_msg = list(data)
        N = settings.midi_channel_number

        idx = 0
        while idx < len(midi_msg):
            status_byte = midi_msg[idx]
            midi_channel = status_byte & 0x0F

            # Check if the message is on our MIDI channel
            if midi_channel != N:
                idx += 1
                continue

            # Check if it's a Control Change message
            if (status_byte & 0xF0) == 0xB0:
                if idx + 2 < len(midi_msg):
                    controller = midi_msg[idx + 1]
                    value = midi_msg[idx + 2]

                    if controller == 0x63:  # Parameter Number MSB
                        if idx + 5 < len(midi_msg):
                            MB = value
                            if (
                                midi_msg[idx + 3] == 0xB0 | N
                                and midi_msg[idx + 4] == 0x62
                            ):  # Parameter Number LSB
                                LB = midi_msg[idx + 5]

                                # Find matching configuration
                                matching_config, channel_type = (
                                    self.find_matching_config(MB, LB)
                                )

                                if matching_config:
                                    dsp_channel_name = matching_config[
                                        "dsp_channel_name"
                                    ]

                                    if channel_type == "level":
                                        value = midi_msg[idx + 8]
                                        self.handle_level_change(
                                            dsp_channel_name, value
                                        )
                                    elif channel_type == "mute":
                                        value = midi_msg[idx + 11]
                                        self.handle_mute_change(dsp_channel_name, value)

                                idx += 9  # Move past this Control Change sequence
                                continue

            idx += 1

        # Update last MIDI message info
        internal_state.last_midi_message = data.hex()
        internal_state.last_midi_message_time = time.time()

    def find_matching_config(self, MB, LB):
        for mapping in settings.mixer_config:
            level_msb = int(mapping["level_msb"], 16)
            level_lsb = int(mapping["level_lsb"], 16)
            mute_msb = int(mapping["mute_msb"], 16)
            mute_lsb = int(mapping["mute_lsb"], 16)

            if (MB == level_msb and LB == level_lsb) or (
                MB == mute_msb and LB == mute_lsb
            ):
                channel_type = (
                    "level" if (MB == level_msb and LB == level_lsb) else "mute"
                )
                return mapping, channel_type
        return None, None

    def handle_level_change(self, dsp_channel_name, level_value):
        if dsp_channel_name not in internal_state.dsp_channel_states:
            internal_state.dsp_channel_states[dsp_channel_name] = {
                "level_value": None,
                "mute_state": None,
            }

        internal_state.dsp_channel_states[dsp_channel_name]["level_value"] = level_value
        internal_state.schedule_save()

        # If bidirectional and DSP connected, send to DSP
        if settings.bidirectional and dsp_communicator.dsp_connected:
            dsp_communicator.send_dsp_message(dsp_channel_name, "level", level_value)

    def handle_mute_change(self, dsp_channel_name, mute_value):
        if dsp_channel_name not in internal_state.dsp_channel_states:
            internal_state.dsp_channel_states[dsp_channel_name] = {
                "level_value": None,
                "mute_state": None,
            }

        mute_state = "on" if mute_value == 1 else "off"
        print(mute_value)
        internal_state.dsp_channel_states[dsp_channel_name]["mute_state"] = mute_state
        internal_state.schedule_save()

        # If bidirectional and DSP connected, send to DSP
        if settings.bidirectional and dsp_communicator.dsp_connected:
            dsp_communicator.send_dsp_message(dsp_channel_name, "mute", mute_state)

    def send_midi_message(self, message_bytes):
        try:
            self.mixer_socket.sendall(message_bytes)
        except Exception as e:
            print(f"Failed to send MIDI message: {e}")
            self.mixer_connected = False

    def send_level_to_mixer(self, mapping, value):
        N = settings.midi_channel_number
        MB = int(mapping["level_msb"], 16)
        LB = int(mapping["level_lsb"], 16)
        # MIDI message format: BN 63 MB BN 62 LB BN 06 VC BN 26 VF
        message = bytes(
            [
                0xB0 | N,
                0x63,
                MB,
                0xB0 | N,
                0x62,
                LB,
                0xB0 | N,
                0x06,
                value,
                0xB0 | N,
                0x26,
                0x00,
            ]
        )
        self.send_midi_message(message)

    def send_mute_to_mixer(self, mapping, mute_state):
        N = settings.midi_channel_number
        MB = int(mapping["mute_msb"], 16)
        LB = int(mapping["mute_lsb"], 16)
        value = 1 if mute_state == "on" else 0
        # Mute on/off message: BN 63 MB BN 62 LB BN 06 00 BN 26 VV
        message = bytes(
            [
                0xB0 | N,
                0x63,
                MB,
                0xB0 | N,
                0x62,
                LB,
                0xB0 | N,
                0x06,
                0x00,
                0xB0 | N,
                0x26,
                value,
            ]
        )
        self.send_midi_message(message)

    def stop(self):
        self.running = False
        if self.mixer_socket:
            self.mixer_socket.close()


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
                    # Compare with internal state
                    if previous_value != level_value or previous_value is None:
                        # If bidirectional and mixer connected, send to mixer
                        if (
                            settings.bidirectional
                            and mixer_communicator.mixer_connected
                        ):
                            # Find corresponding mixer mapping
                            mapping = self.find_mixer_mapping(dsp_channel_name)
                            if mapping:
                                mixer_communicator.send_level_to_mixer(
                                    mapping, level_value
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
                    # Compare with internal state
                    if previous_state != mute_state or previous_state is None:
                        # If bidirectional and mixer connected, send to mixer
                        if (
                            settings.bidirectional
                            and mixer_communicator.mixer_connected
                        ):
                            mapping = self.find_mixer_mapping(dsp_channel_name)
                            if mapping:
                                mixer_communicator.send_mute_to_mixer(
                                    mapping, mute_state
                                )
                # Handle flash indication
                if settings.flash_indication and settings.main_on:
                    current_time = time.time()
                    if current_time - self.last_flash_time > 5:
                        mapping = self.find_mixer_mapping(dsp_channel_name)
                        if mapping:
                            N = settings.midi_channel_number
                            MB = int(mapping["mute_msb"], 16)
                            LB = int(mapping["mute_lsb"], 16)
                            # Mute toggle message: BN 63 MB BN 62 LB BN 60 00
                            message = bytes(
                                [
                                    0xB0 | N,
                                    0x63,
                                    MB,
                                    0xB0 | N,
                                    0x62,
                                    LB,
                                    0xB0 | N,
                                    0x60,
                                    0x00,
                                ]
                            )
                            # Send the message twice with 100ms delay
                            mixer_communicator.send_midi_message(message)
                            time.sleep(0.1)
                            mixer_communicator.send_midi_message(message)
                            self.last_flash_time = current_time
        else:
            print(f"Received unexpected DSP message length: {len(data)}")

    def find_mixer_mapping(self, dsp_channel_name):
        for mapping in settings.mixer_config:
            if mapping["dsp_channel_name"] == dsp_channel_name:
                return mapping
        return None

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
                    f"send {message_type} message to dsp: {message.hex()}, mute state {value}"
                )
            except Exception as e:
                print(f"Failed to send DSP message: {e}")

    def stop(self):
        self.running = False
        if self.dsp_socket:
            self.dsp_socket.close()


# Instantiate communicators
mixer_communicator = MixerCommunicator()
dsp_communicator = DSPCommunicator()
mixer_communicator.start()
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
        settings.mixer_ip = request.form.get("mixer_ip", settings.mixer_ip)
        settings.dsp_ip = request.form.get("dsp_ip", settings.dsp_ip)
        settings.main_on = "main_on" in request.form
        settings.bidirectional = "bidirectional" in request.form
        settings.flash_indication = "flash_indication" in request.form
        settings.midi_channel_number = int(
            request.form.get("midi_channel_number", settings.midi_channel_number)
        )
        # Handle mixer_config and dsp_config tables
        settings.mixer_config = json.loads(request.form.get("mixer_config", "[]"))
        settings.dsp_config = json.loads(request.form.get("dsp_config", "[]"))
        settings.save_settings()
        return redirect(url_for("index"))
    else:
        # Render the index page with current settings and state
        return render_template(
            "index.html",
            settings=settings,
            internal_state=internal_state,
            mixer_status=get_mixer_status(),
            dsp_status=get_dsp_status(),
        )


@app.route("/restart_device", methods=["POST"])
def restart_device():
    # Restart the Raspberry Pi Zero
    os.system("sudo reboot")
    return "Restarting device...", 200


@app.route("/api/state")
def api_state():
    # Return the internal state and last MIDI message as JSON
    last_midi_message_time = internal_state.last_midi_message_time
    if last_midi_message_time:
        time_since_last_midi = time.time() - last_midi_message_time
    else:
        time_since_last_midi = None
    return jsonify(
        {
            "dsp_channel_states": internal_state.dsp_channel_states,
            "last_midi_message": internal_state.last_midi_message,
            "time_since_last_midi": time_since_last_midi,
            "mixer_status": get_mixer_status(),
            "dsp_status": get_dsp_status(),
        }
    )


@app.route("/manual.pdf")
def get_manual():
    return redirect(
        "https://www.allen-heath.com/content/uploads/2023/11/SQ-MIDI-Protocol-Issue5.pdf"
    )


def get_mixer_status():
    if mixer_communicator.mixer_connected:
        if (
            internal_state.last_midi_message_time
            and time.time() - internal_state.last_midi_message_time < 60
        ):
            return "connected (receiving midi)"
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
