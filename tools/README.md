# Tools

- **claude_wrapper.py**: A PTY wrapper using pexpect and bleak. It replaces the macOS/Windows desktop application by running claude (Claude Code) in a child PTY, parsing its output to detect prompts, and maintaining the exact same BLE JSON stream interface with the Hardware Buddy. Use this if you have Anthropic API credits but no Claude.ai desktop subscription.
- **prep_character.py**: Converts standard GIF files into the optimized format expected by the device.
- **flash_character.py**: Flashes a character directly via USB filesystem without having to use the BLE folder push drop-target.
- **test_serial.py** / **test_xfer.py**: Quick test scripts to mock the bridging protocol over USB.
