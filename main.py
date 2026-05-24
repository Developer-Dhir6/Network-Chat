# main.py
# Wires discovery + hub together. Terminal chat — UI comes in Stage 4.

import time
import threading
from discovery import DiscoveryManager
from hub_manager import HubManager
import random

def main():
    username = f"anon_{random.randint(100, 999)}"
    print(f"Starting as: {username}\n")

    # ── Step 1: Start discovery ──────────────────────────────────────────────
    dm = DiscoveryManager(username=username)
    dm.start()

    print("Collecting peers for 4 seconds...")
    time.sleep(4)

    peers = dm.get_peers()
    print(f"Peers found before election: {list(peers.keys()) or ['none — will be hub']}\n")

    # ── Step 2: Run election + start hub/client ──────────────────────────────
    def on_message(sender, text):
        print(f"\n  [{sender}]: {text}")
        print("  You: ", end="", flush=True)

    hm = HubManager(
        my_ip=dm.my_ip,
        username=username,
        on_message=on_message
    )

    # start() blocks for up to ELECTION_TIMEOUT seconds while election runs
    hm.start(dm)

    time.sleep(1)   # Give TCP connection a moment to establish

    # ── Step 3: Input loop ───────────────────────────────────────────────────
    print("\nChat ready. Type messages and press Enter. Ctrl+C to quit.\n")
    try:
        while True:
            print("  You: ", end="", flush=True)
            text = input()
            if text.strip():
                hm.send_message(text.strip())
    except KeyboardInterrupt:
        print("\nShutting down...")
        hm.stop()
        dm.stop()

if __name__ == "__main__":
    main()
