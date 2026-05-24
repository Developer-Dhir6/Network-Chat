# discovery.py
# Stage 1: UDP Peer Discovery — Bulletproof Edition
#
# Two-layer discovery:
#   Layer 1 — Subnet broadcast: "is anyone out there?"
#   Layer 2 — Direct unicast:   once we know a peer's IP, ping them directly
#
# This means discovery works even on routers that drop broadcast packets,
# or networks with AP isolation — as long as peers find each other once,
# they stay connected via direct pings forever after.

import socket
import threading
import time
import json
import struct

# ─── Constants ───────────────────────────────────────────────────────────────

BROADCAST_PORT       = 50000
BROADCAST_INTERVAL   = 3         # seconds between broadcasts
DIRECT_PING_INTERVAL = 4         # seconds between direct pings to known peers
PEER_TIMEOUT         = 15        # drop peer if silent for this long
BUFFER_SIZE          = 1024

# ─── Discovery Manager ───────────────────────────────────────────────────────

class DiscoveryManager:
    """
    Bulletproof peer discovery using two complementary mechanisms:

    1. Broadcast (finds NEW peers):
       Sends UDP to the subnet broadcast address (e.g. 192.168.31.255) every
       BROADCAST_INTERVAL seconds. Every peer on the LAN receives this.
       Also sends to 255.255.255.255 as a fallback for unusual network configs.

    2. Direct unicast (keeps KNOWN peers alive):
       Once we know a peer's IP, we send HELLO directly to them every
       DIRECT_PING_INTERVAL seconds. This bypasses broadcast entirely and
       works even if the router drops every broadcast packet.

    Result: flaky broadcast only affects initial discovery. After two peers
    find each other once, they stay discovered indefinitely via direct pings.
    """

    def __init__(self, username: str, on_peer_joined=None, on_peer_left=None):
        self.username       = username
        self.on_peer_joined = on_peer_joined
        self.on_peer_left   = on_peer_left

        self.peers: dict    = {}                # ip -> {username, last_seen}
        self.peers_lock     = threading.Lock()
        self.running        = False

        self.my_ip          = self._get_local_ip()
        self.broadcast_addr = self._get_broadcast_addr(self.my_ip)

        print(f"[Discovery] My IP:        {self.my_ip}")
        print(f"[Discovery] Broadcast to: {self.broadcast_addr}")

    # ─── Network Info ─────────────────────────────────────────────────────

    def _get_local_ip(self) -> str:
        """Get our LAN IP by routing trick."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"
        finally:
            s.close()

    def _get_broadcast_addr(self, ip: str, mask: str = "255.255.255.0") -> str:
        """
        Calculate the subnet broadcast address from an IP and mask.

        How it works:
          IP:            192.168.31.80   -> packed to 4 bytes
          Mask:          255.255.255.0   -> packed to 4 bytes
          Inverted mask: 0.0.0.255       -> ~mask (bitwise NOT)
          Broadcast:     IP | ~mask      -> 192.168.31.255

        We hardcode /24 (255.255.255.0) as the default because it covers
        99% of home and university networks. If someone is on a /16 or
        unusual subnet, the fallback 255.255.255.255 broadcast still fires.
        """
        try:
            ip_int    = struct.unpack("!I", socket.inet_aton(ip))[0]
            mask_int  = struct.unpack("!I", socket.inet_aton(mask))[0]
            bcast_int = ip_int | (~mask_int & 0xFFFFFFFF)
            return socket.inet_ntoa(struct.pack("!I", bcast_int))
        except Exception:
            return "255.255.255.255"    # safe fallback

    # ─── Start / Stop ─────────────────────────────────────────────────────

    def start(self):
        self.running = True

        threads = [
            threading.Thread(target=self._listener,      daemon=True, name="DiscoveryListener"),
            threading.Thread(target=self._broadcaster,   daemon=True, name="DiscoveryBroadcaster"),
            threading.Thread(target=self._direct_pinger, daemon=True, name="DiscoveryDirectPinger"),
            threading.Thread(target=self._cleanup_loop,  daemon=True, name="DiscoveryCleanup"),
        ]

        # Listener must be ready before broadcaster fires
        threads[0].start()
        time.sleep(0.1)
        for t in threads[1:]:
            t.start()

        print(f"[Discovery] Started as '{self.username}'")

    def stop(self):
        self.running = False
        print("[Discovery] Stopped.")

    # ─── Broadcaster (Layer 1) ────────────────────────────────────────────

    def _broadcaster(self):
        """
        Sends HELLO to the subnet broadcast address every BROADCAST_INTERVAL seconds.
        Also sends to 255.255.255.255 as a secondary attempt.
        Bound to self.my_ip so packets are stamped with our real LAN IP.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.my_ip, 0))      # bind to real IP — critical for correct sender_ip

        while self.running:
            packet = self._make_hello_packet()
            for target in [self.broadcast_addr, "255.255.255.255"]:
                try:
                    sock.sendto(packet, (target, BROADCAST_PORT))
                except Exception as e:
                    print(f"[Discovery] Broadcast error to {target}: {e}")

            time.sleep(BROADCAST_INTERVAL)

        sock.close()

    # ─── Direct Pinger (Layer 2) ──────────────────────────────────────────

    def _direct_pinger(self):
        """
        For every peer we already know about, send HELLO directly to their IP.
        This keeps known peers alive even if broadcast stops working entirely.
        Uses a fresh snapshot of peers each cycle so we never hold the lock
        while doing network I/O.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.my_ip, 0))

        while self.running:
            time.sleep(DIRECT_PING_INTERVAL)

            with self.peers_lock:
                known_ips = list(self.peers.keys())     # snapshot — release lock immediately

            packet = self._make_hello_packet()
            for ip in known_ips:
                try:
                    sock.sendto(packet, (ip, BROADCAST_PORT))
                except Exception as e:
                    print(f"[Discovery] Direct ping error to {ip}: {e}")

        sock.close()

    # ─── Listener ─────────────────────────────────────────────────────────

    def _listener(self):
        """
        Handles ALL incoming HELLO packets — both broadcast and direct unicast.
        They all arrive on the same port 50000 so one listener handles both.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", BROADCAST_PORT))
        sock.settimeout(1.0)

        print(f"[Discovery] Listening on port {BROADCAST_PORT}")

        while self.running:
            try:
                data, (sender_ip, _) = sock.recvfrom(BUFFER_SIZE)
            except socket.timeout:
                continue
            except Exception as e:
                print(f"[Discovery] Listener error: {e}")
                continue

            if sender_ip == self.my_ip:
                continue    # ignore our own packets

            try:
                packet = json.loads(data.decode("utf-8"))
            except json.JSONDecodeError:
                continue

            if packet.get("type") == "HELLO":
                self._handle_hello(sender_ip, packet)

        sock.close()

    def _handle_hello(self, sender_ip: str, packet: dict):
        """Update peer list. Fires on_peer_joined callback for new peers."""
        username = packet.get("username", "unknown")

        with self.peers_lock:
            is_new = sender_ip not in self.peers
            self.peers[sender_ip] = {
                "username": username,
                "last_seen": time.time()
            }

        if is_new:
            print(f"[Discovery] Peer joined: {username} ({sender_ip})")
            if self.on_peer_joined:
                self.on_peer_joined(sender_ip, username)

    # ─── Cleanup ──────────────────────────────────────────────────────────

    def _cleanup_loop(self):
        while self.running:
            time.sleep(PEER_TIMEOUT / 2)
            self._remove_stale_peers()

    def _remove_stale_peers(self):
        now   = time.time()
        stale = []

        with self.peers_lock:
            for ip, info in self.peers.items():
                if now - info["last_seen"] > PEER_TIMEOUT:
                    stale.append((ip, info["username"]))
            for ip, _ in stale:
                del self.peers[ip]

        for ip, username in stale:
            print(f"[Discovery] Peer left (timeout): {username} ({ip})")
            if self.on_peer_left:
                self.on_peer_left(ip, username)

    # ─── Helpers ──────────────────────────────────────────────────────────

    def _make_hello_packet(self) -> bytes:
        return json.dumps({
            "type":     "HELLO",
            "username": self.username,
            "ip":       self.my_ip
        }).encode("utf-8")

    def get_peers(self) -> dict:
        """Returns a thread-safe snapshot of current peers."""
        with self.peers_lock:
            return dict(self.peers)


# ─── Test Harness ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import random

    username = f"anon_{random.randint(100, 999)}"

    def peer_joined(ip, name):
        print(f"  *** {name} joined ({ip})")

    def peer_left(ip, name):
        print(f"  *** {name} left ({ip})")

    dm = DiscoveryManager(
        username=username,
        on_peer_joined=peer_joined,
        on_peer_left=peer_left
    )
    dm.start()

    try:
        while True:
            time.sleep(5)
            peers = dm.get_peers()
            print(f"\n[Peers online — {len(peers)} total]")
            for ip, info in peers.items():
                print(f"  {info['username']} @ {ip}")
    except KeyboardInterrupt:
        dm.stop()
