# SQ7-DSP Bridge

A bridge application to connect Allen & Heath SQ7 mixer with a Symetrix SymNet Solus 8 DSP system via MIDI over IP. It may also work with other hardware, but this has not been tested.

## Features

- Bi-directional communication between SQ7 mixer and DSP
- Web interface for configuration and monitoring
- Real-time status updates
- Persistent configuration storage
- Automatic device discovery
- Visual feedback on mixer for successful changes

## System Requirements

- Raspberry Pi Zero (or any other hardware; runs on Windows and Linux)
- Python 3.x
- Network connection (WiFi or LAN)

## Web Interface

### Configuration Options
- Mixer and DSP IP addresses
- Main power switch
- Bi-directional control toggle
- Mixer flash indication toggle
- MIDI channel selection
- Device restart button

### Channel Mapping
Configure the relationship between mixer channels and DSP channels:
- Mixer channel configuration (MSB/LSB for level and mute)
- DSP channel selection
- Dynamic row addition/removal

### Status Display
- Connection status for both devices
- Last received MIDI message with highlighting
- Time since last MIDI message
- Internal state display
- Real-time updates

## Technical Details

### DSP Protocol
- UDP communication
- Port: 49152
- 20-byte payload format
- Example payload: `00 00 00 02 00 00 00 14 00 00 00 01 03 00 00 XX YY ZZ 00 00`

### MIDI Implementation
- Port: 51325
- MIDI over TCP/IP
- Mute message format: `BN 63 MB BN 62 LB BN 06 00 BN 26 [01|00]`
- Level message format: `BN 63 MB BN 62 LB BN 06 VC BN 26 VF`

For detailed MIDI implementation, see the [SQ MIDI Protocol Guide](https://www.allen-heath.com/content/uploads/2023/11/SQ-MIDI-Protocol-Issue5.pdf)

## Operation

### Startup Sequence
1. Device discovery (5-second intervals)
2. MIDI initialization
3. State synchronization
4. Web interface activation

### State Management
- 5-second delay before saving changes
- Undefined state handling
- Automatic state synchronization
- Flash indication with 5-second cooldown

## Reference Links

- [SQ MIDI Protocol Documentation](https://www.allen-heath.com/content/uploads/2023/11/SQ-MIDI-Protocol-Issue5.pdf)

---

This project implements a bridge between an Allen & Heath SQ7 mixer and a DSP system, providing real-time synchronization and web-based configuration.
