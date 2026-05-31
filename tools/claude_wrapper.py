#!/usr/bin/env python3
"""Bridge a real Claude Code session to the Hardware Buddy over BLE.

Spawns `claude` (or any command) inside a PTY, proxies it transparently to
your terminal, and in parallel:
  - pushes session state to the device (NUS RX characteristic), and
  - watches the output for Claude Code's interactive *permission* prompt,
    forwarding it to the device so you can approve/deny from the stick.

When the device sends back a decision, we inject the matching keystroke into
the PTY: Enter to confirm the default-highlighted "Yes" (approve), or Esc to
cancel (deny).
"""
import asyncio
import hashlib
import json
import os
import re
import signal
import sys
import termios
import time
import tty

from bleak import BleakScanner, BleakClient
import pexpect

# UUIDs from REFERENCE.md
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_UUID      = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # desktop -> device
NUS_TX_UUID      = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # device -> desktop

# --- Keystrokes injected into the Claude Code PTY per remote decision --------
# Claude Code's permission prompt is a select menu whose first option ("1.
# Yes") is highlighted by default, so Enter confirms it. Esc cancels the
# request (the "deny" path). These are deliberately default-selection based so
# we don't depend on whether number keys auto-submit in your build. To approve
# a *non-default* option instead, set KEYS_APPROVE to e.g. "2\r".
KEYS_APPROVE = "\r"      # confirm highlighted "Yes"
KEYS_DENY    = "\x1b"    # Esc -> cancel the permission request
# "Yes, and don't ask again" is the item right below the default "1. Yes".
# Navigate by arrow (Down once) + Enter rather than typing "2": this is
# robust whether the menu has 2, 3, or 4 options, lands on the *narrowest*
# don't-ask-again scope, and never auto-submits a stray digit. The device
# sends this via a BtnA long-press.
KEYS_ALWAYS  = "\x1b[B\r"   # Down, then Enter

# Firmware buffer sizes (src/data.h) — clamp so nothing is silently truncated
# mid-multibyte or overruns the device-side strncpy.
MAX_ID, MAX_TOOL, MAX_HINT, MAX_LINE, MAX_MSG = 39, 19, 43, 91, 23

# Strip CSI/SGR, OSC, and single-char escape sequences. The device only needs
# the literal text that appears in the frame; this keeps detection robust
# against color codes and cursor moves.
ANSI_RE = re.compile(
    r'\x1B(?:'
    r'\[[0-?]*[ -/]*[@-~]'          # CSI ... cmd  (colors, cursor moves)
    r'|\][^\x07\x1B]*(?:\x07|\x1B\\)'  # OSC ... BEL/ST
    r'|[@-Z\\-_]'                   # 2-char escapes
    r')'
)

# The permission menu, after stripping ANSI, always contains a numbered option
# list. Require the whole cluster so ordinary "1. Yes" prose in Claude's output
# doesn't trip it.
OPT1_RE   = re.compile(r'\b1\.\s*Yes', re.I)
OPT_NO_RE = re.compile(r'\b[23]\.\s*\S', re.I)
QUESTION_RE = re.compile(r'Do you want[^?\n]*\?', re.I)
# The selection cursor sitting on a numbered option (e.g. "❯ 1.") is the
# defining feature of the interactive select menu and never appears in prose.
ARROW_OPT_RE = re.compile(r'❯\s*[1-9]\.')
# The literal escape hint Claude prints on the cancel option. Matched as a
# whole token (parenthesized, or "esc to ...") so it can't fire on ordinary
# words like "describe", "escape", or "rescue".
ESC_HINT_RE  = re.compile(r'\(esc\)|\besc to\b', re.I)


def clamp(s, n):
    return s[:n] if s else ""


def infer_tool(question):
    q = question.lower()
    if "edit" in q or "change" in q or "modify" in q:        return "Edit"
    if "create" in q or "write" in q or " file" in q:        return "Write"
    if "run" in q or "command" in q or "bash" in q or "execute" in q:
        return "Bash"
    if "read" in q:                                          return "Read"
    if "fetch" in q or "url" in q or "web" in q:             return "Fetch"
    if "proceed" in q or "trust" in q:                       return "Tool use"
    return "Claude Code"


class BuddyBridge:
    def __init__(self, command):
        self.command = command
        self.child = None
        self.client = None
        self.loop = None
        self.state = {
            "total": 0, "running": 0, "waiting": 0,
            "msg": "Initializing...", "entries": [],
            "tokens": 0, "tokens_today": 0, "prompt": {},
        }
        self.last_heartbeat = 0.0
        # Prompt lifecycle
        self.prompt_active = False
        self.current_sig = None     # signature of the on-screen prompt
        self.answered_sig = None    # suppress re-firing a just-answered frame
        self.prompt_seq = 0
        self._win = ""              # bounded, ANSI-stripped rolling window
        self._last_entry = ""

    # ---- BLE ----------------------------------------------------------------
    async def find_device(self):
        print("Scanning for Hardware Buddy (Claude)...")
        devices = await BleakScanner.discover()
        for d in devices:
            if d.name and d.name.startswith("Claude"):
                return d
        return None

    def notification_handler(self, sender, data):
        try:
            msg = data.decode("utf-8").strip()
            if not msg:
                return
            for line in msg.split("\n"):
                if not line.strip():
                    continue
                obj = json.loads(line)
                if obj.get("cmd") != "permission":
                    continue
                decision = obj.get("decision")
                if not (self.prompt_active and self.child and self.child.isalive()):
                    continue
                if decision not in ("once", "always", "deny"):
                    continue
                # Guard: only inject a keystroke if a genuine permission menu is
                # STILL on screen and matches the prompt we advertised. This
                # stops a remote button press from leaking a stray key into the
                # session when the earlier detection was a false positive, or
                # the menu has since scrolled away / been answered locally.
                det = self.detect_prompt(self._win)
                if not det or det[0] != self.current_sig:
                    print("\r\n[Hardware Buddy]: no live menu — keystroke "
                          "suppressed\r")
                    self._clear_prompt("Running...")
                    if self.loop:
                        self.loop.call_soon_threadsafe(
                            lambda: asyncio.ensure_future(self.send_state()))
                    continue
                if decision == "once":
                    print("\r\n[Hardware Buddy]: Approved!\r")
                    self.child.send(KEYS_APPROVE)
                elif decision == "always":
                    print("\r\n[Hardware Buddy]: Approved (don't ask again)!\r")
                    self.child.send(KEYS_ALWAYS)
                elif decision == "deny":
                    print("\r\n[Hardware Buddy]: Denied!\r")
                    self.child.send(KEYS_DENY)
                # Remote answer accepted: stop advertising this prompt and
                # remember its signature so the lingering frame doesn't re-fire.
                self._clear_prompt("Processing...", answered=True)
                if self.loop:
                    self.loop.call_soon_threadsafe(
                        lambda: asyncio.ensure_future(self.send_state()))
        except Exception as e:
            print(f"\r\nError handling notification: {e}\r")

    async def send_state(self):
        if not self.client or not self.client.is_connected:
            return
        payload = {
            "total": self.state["total"],
            "running": self.state["running"],
            "waiting": self.state["waiting"],
            "msg": clamp(self.state["msg"], MAX_MSG),
            "entries": self.state["entries"][-8:],
        }
        # Include "prompt" ONLY while one is pending — the firmware clears the
        # device-side prompt whenever the key is absent (src/data.h:115).
        if self.state["prompt"]:
            payload["prompt"] = self.state["prompt"]
        msg = json.dumps(payload) + "\n"
        try:
            await self.client.write_gatt_char(NUS_RX_UUID, msg.encode("utf-8"))
            self.last_heartbeat = time.time()
        except Exception as e:
            print(f"\r\nBLE write failed: {e}\r")

    def _clear_prompt(self, msg, answered=False):
        """Drop the pending prompt locally; the next send_state pushes {}.

        answered=True records the current signature so the menu's lingering
        redraw frame can't immediately re-fire, and wipes the detection window
        so stale menu text can't resurrect the prompt on a later chunk.
        """
        if answered:
            self.answered_sig = self.current_sig
            self._win = ""
        self.prompt_active = False
        self.current_sig = None
        self.state["prompt"] = {}
        self.state["waiting"] = 0
        self.state["running"] = 1
        self.state["msg"] = msg

    # ---- Prompt detection ---------------------------------------------------
    def detect_prompt(self, window):
        """Return (signature, tool, hint) if Claude's permission menu is
        genuinely on screen, else None.

        Only the freshest slice of output is inspected, so stale numbered lists
        or an earlier question can't resurrect a phantom prompt. The gate
        requires the real interactive-menu structure rather than loose
        substrings that appear in ordinary prose:
          - a "Do you want ...?" question,
          - a tight "1. Yes" / "2."|"3." option cluster, and
          - an unambiguous menu marker: the selection cursor on an option
            (❯ 1.) or the literal "(esc)" / "esc to" cancel hint.
        """
        region = window[-2500:]   # only what's plausibly still on screen
        m1s = list(OPT1_RE.finditer(region))
        if not m1s:
            return None
        # The option cluster must be tight — a "2."/"3." option within a few
        # lines of the "1. Yes", not scattered across unrelated output.
        m1 = m1s[-1]
        if not OPT_NO_RE.search(region, m1.end(), m1.end() + 400):
            return None
        if not QUESTION_RE.search(region):
            return None
        if not (ARROW_OPT_RE.search(region) or ESC_HINT_RE.search(region)):
            return None

        # Use the freshest question (last frame in the rolling window).
        question = re.sub(r'[^\x20-\x7e]', ' ', QUESTION_RE.findall(region)[-1])
        question = re.sub(r'\s+', ' ', question).strip()

        tool = infer_tool(question)
        hint = re.sub(r'^do you want to\s*', '', question, flags=re.I).rstrip("?")
        hint = hint.strip() or question
        sig = hashlib.md5(question.encode("utf-8", "ignore")).hexdigest()[:12]
        return sig, clamp(tool, MAX_TOOL), clamp(hint, MAX_HINT)

    def _push_entry(self, text):
        text = re.sub(r'[^\x20-\x7e]', '', text).strip()
        # Skip noise: too short, mostly punctuation, or the box chrome.
        if len(text) < 6 or text == self._last_entry:
            return
        letters = sum(c.isalpha() for c in text)
        if letters < 3:
            return
        self._last_entry = text
        self.state["entries"].insert(0, clamp(text, MAX_LINE))
        if len(self.state["entries"]) > 8:
            self.state["entries"].pop()

    async def _on_frame(self, clean_chunk):
        """Update rolling window + prompt lifecycle from a cleaned chunk."""
        self._win = (self._win + clean_chunk)[-6000:]
        det = self.detect_prompt(self._win)

        if det:
            sig, tool, hint = det
            if sig == self.answered_sig:
                return  # lingering frame of a prompt we already answered
            if not self.prompt_active or sig != self.current_sig:
                self.prompt_active = True
                self.current_sig = sig
                self.answered_sig = None
                self.prompt_seq += 1
                self.state["prompt"] = {
                    "id": f"p{self.prompt_seq}",
                    "tool": tool,
                    "hint": hint,
                }
                self.state["waiting"] = 1
                self.state["running"] = 0
                self.state["msg"] = "Approval needed"
                await self.send_state()
        else:
            # No menu on screen.
            self.answered_sig = None
            if self.prompt_active:
                # Answered locally (in the terminal) — clear it on the device.
                self.prompt_active = False
                self.current_sig = None
                self.state["prompt"] = {}
                self.state["waiting"] = 0
                self.state["running"] = 1
                self.state["msg"] = "Running..."
                await self.send_state()

    # ---- PTY loop -----------------------------------------------------------
    async def process_pty(self):
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        loop = asyncio.get_event_loop()
        self.loop = loop

        try:
            tty.setraw(fd)

            cols, rows = 80, 24
            try:
                sz = os.get_terminal_size(fd)
                cols, rows = sz.columns, sz.lines
            except OSError:
                pass

            # codec_errors='ignore' so a multibyte glyph (e.g. "❯") split across
            # a read boundary can't crash the decoder. dimensions so the Ink TUI
            # lays out correctly.
            self.child = pexpect.spawn(
                self.command[0], args=self.command[1:],
                encoding="utf-8", codec_errors="ignore",
                dimensions=(rows, cols),
            )

            def on_winch(*_):
                try:
                    s = os.get_terminal_size(fd)
                    if self.child and self.child.isalive():
                        self.child.setwinsize(s.lines, s.columns)
                except Exception:
                    pass
            signal.signal(signal.SIGWINCH, on_winch)

            input_queue = asyncio.Queue()

            def on_stdin():
                try:
                    data = os.read(fd, 1024).decode("utf-8", errors="ignore")
                    if data:
                        input_queue.put_nowait(data)
                except Exception:
                    pass
            loop.add_reader(fd, on_stdin)

            while True:
                if not self.child.isalive():
                    self.state.update(total=0, running=0, waiting=0,
                                      msg="Session ended.", prompt={})
                    await self.send_state()
                    break

                while not input_queue.empty():
                    data = input_queue.get_nowait()
                    if self.child.isalive():
                        self.child.send(data)

                try:
                    chunk = self.child.read_nonblocking(size=4096, timeout=0)
                    sys.stdout.write(chunk)
                    sys.stdout.flush()

                    clean = ANSI_RE.sub("", chunk)
                    await self._on_frame(clean)

                    # Best-effort transcript: feed device a few content lines.
                    if not self.prompt_active:
                        for raw in clean.replace("\r", "\n").split("\n"):
                            self._push_entry(raw)
                        self.state["total"] = max(self.state["total"], 1)
                        self.state["running"] = max(self.state["running"], 1)
                        if self.state["msg"] in ("Initializing...", "Connected!"):
                            self.state["msg"] = "Running..."
                        if time.time() - self.last_heartbeat > 0.5:
                            await self.send_state()

                except pexpect.TIMEOUT:
                    await asyncio.sleep(0.01)
                except pexpect.EOF:
                    break

                if time.time() - self.last_heartbeat > 5:
                    await self.send_state()

        except Exception as e:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            print(f"\r\nPTY error: {e}\r")
        finally:
            try:
                loop.remove_reader(fd)
            except Exception:
                pass
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    async def run(self):
        device = await self.find_device()
        if not device:
            print("Device not found. Is it on and advertising 'Claude'?")
            return

        print(f"Connecting to {device.name} ({device.address})...")
        async with BleakClient(device) as client:
            self.client = client
            print("Connected! Starting PTY...")

            await client.start_notify(NUS_TX_UUID, self.notification_handler)

            # Initial sync: clock + owner name.
            tz = -time.timezone if time.localtime().tm_isdst == 0 else -time.altzone
            sync = json.dumps({"time": [int(time.time()), tz]}) + "\n"
            await client.write_gatt_char(NUS_RX_UUID, sync.encode("utf-8"))
            owner = json.dumps({"cmd": "owner", "name": "Maker"}) + "\n"
            await client.write_gatt_char(NUS_RX_UUID, owner.encode("utf-8"))

            self.state.update(total=1, running=1, msg="Connected!")
            await self.send_state()

            await self.process_pty()

            print("Cleaning up...")
            await client.stop_notify(NUS_TX_UUID)


async def main():
    if len(sys.argv) < 2:
        print("Usage: ./claude_wrapper.py <command>")
        print("Example: ./claude_wrapper.py claude")
        sys.exit(1)
    await BuddyBridge(sys.argv[1:]).run()


if __name__ == "__main__":
    asyncio.run(main())
