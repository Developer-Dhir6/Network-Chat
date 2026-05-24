# hub_manager.py
# Stage 2 (fixed): TCP Group Chat — Hub Beacon + Rejoin Edition
#
# Problems fixed:
#   Bug 1 — Hub restarts but client never reconnects:
#     Old code: client got WinError 10054, printed it, stopped. Done.
#     Fix: client detects dead connection → re-enters listen phase → reconnects.
#
#   Bug 2 — Two hubs elected simultaneously:
#     Old code: fixed 4s discovery window. If peer B started after that window,
#     it found no peers, elected itself hub, and ran parallel to peer A's hub.
#     Fix: BEFORE running any election, every peer listens for 5 seconds for a
#     HUB_ALIVE beacon. If one exists, skip election and connect directly.
#     The hub broadcasts HUB_ALIVE every 3s, so any late joiner finds it fast.
#
# New flow:
#   App start
#     └─ listen for HUB_ALIVE beacon (5s)
#           ├─ beacon heard  → connect as client (no election needed)
#           └─ silence       → run election → winner becomes hub, others connect
#
#   Hub dies (client gets WinError / connection reset)
#     └─ re-enter listen phase (same as app start)
#           ├─ hub restarted  → connect as client
#           └─ still silent   → run election again

import socket
import threading
import json
import time
import struct

CHAT_PORT          = 50001
ELECTION_PORT      = 50003          # also used for HUB_ALIVE beacons
BUFFER_SIZE        = 4096
ELECTION_TIMEOUT   = 6              # seconds to collect votes
HUB_LISTEN_TIMEOUT = 5              # seconds to wait for a HUB_ALIVE beacon
HUB_BEACON_INTERVAL = 3             # hub broadcasts this often


class HubManager:
    def __init__(self, my_ip: str, username: str, on_message=None):
        self.my_ip      = my_ip
        self.username   = username
        self.on_message = on_message

        self.is_hub     = False
        self.hub_ip     = None
        self.running    = False

        self.client_sockets: dict = {}
        self.clients_lock = threading.Lock()
        self.hub_socket   = None

        self.election_votes: dict = {}
        self.election_lock  = threading.Lock()

    # ─── IP Utility ───────────────────────────────────────────────────────

    @staticmethod
    def _ip_key(ip: str):
        return tuple(int(p) for p in ip.split("."))

    def _get_broadcast_addr(self) -> str:
        try:
            ip_int   = struct.unpack("!I", socket.inet_aton(self.my_ip))[0]
            mask_int = struct.unpack("!I", socket.inet_aton("255.255.255.0"))[0]
            bcast    = ip_int | (~mask_int & 0xFFFFFFFF)
            return socket.inet_ntoa(struct.pack("!I", bcast))
        except Exception:
            return "255.255.255.255"

    # ─── Hub Beacon (hub only) ────────────────────────────────────────────
    #
    # The hub broadcasts a HUB_ALIVE packet every HUB_BEACON_INTERVAL seconds.
    # This is what solves Bug 2: any peer that joins after election can hear
    # this beacon during its HUB_LISTEN_TIMEOUT window and skip election entirely.

    def _beacon_loop(self):
        """Runs only on the hub. Keeps broadcasting so late joiners find it."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.my_ip, 0))

        bcast = self._get_broadcast_addr()
        packet = json.dumps({
            "type":   "HUB_ALIVE",
            "hub_ip": self.my_ip
        }).encode("utf-8")

        while self.running and self.is_hub:
            for target in [bcast, "255.255.255.255"]:
                try:
                    sock.sendto(packet, (target, ELECTION_PORT))
                except Exception:
                    pass
            # Also unicast directly to all known clients
            with self.clients_lock:
                known = list(self.client_sockets.keys())
            for ip in known:
                try:
                    sock.sendto(packet, (ip, ELECTION_PORT))
                except Exception:
                    pass
            time.sleep(HUB_BEACON_INTERVAL)

        sock.close()

    # ─── Listen for existing hub ───────────────────────────────────────────
    #
    # Called at startup AND after a hub disconnection.
    # Returns the hub's IP if one is found, or None if silence.
    #
    # Why this solves Bug 2:
    #   Peer B starts, listens for 5s. Hub is already running and beaconing
    #   every 3s. B hears HUB_ALIVE, gets the hub's IP, connects directly.
    #   No election. No second hub.

    def _listen_for_hub(self) -> str | None:
        """
        Opens a UDP socket on ELECTION_PORT and waits up to HUB_LISTEN_TIMEOUT
        seconds for a HUB_ALIVE packet. Returns the hub IP if found, else None.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", ELECTION_PORT))
        sock.settimeout(1.0)

        print(f"[Hub] Listening for existing hub ({HUB_LISTEN_TIMEOUT}s)...")
        deadline = time.time() + HUB_LISTEN_TIMEOUT

        found_ip = None
        while time.time() < deadline:
            try:
                data, (sender_ip, _) = sock.recvfrom(BUFFER_SIZE)
            except socket.timeout:
                continue
            except Exception:
                continue

            try:
                packet = json.loads(data.decode("utf-8"))
            except json.JSONDecodeError:
                continue

            if packet.get("type") == "HUB_ALIVE":
                found_ip = packet.get("hub_ip", sender_ip)
                print(f"[Hub] Existing hub found: {found_ip}")
                break

        sock.close()
        return found_ip

    # ─── Election (unchanged logic, same as before) ────────────────────────

    def _compute_my_vote(self, peers: dict) -> str:
        all_ips = list(peers.keys()) + [self.my_ip]
        return min(all_ips, key=self._ip_key)

    def _run_election(self, discovery_manager) -> str:
        peers   = discovery_manager.get_peers()
        my_vote = self._compute_my_vote(peers)

        with self.election_lock:
            self.election_votes = {self.my_ip: my_vote}   # reset votes each election

        print(f"[Election] Starting. I vote for: {my_vote}")

        listener = threading.Thread(
            target=self._election_listener, daemon=True, name="ElectionListener"
        )
        listener.start()

        bcast_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        bcast_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        bcast_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        bcast_sock.bind((self.my_ip, 0))

        bcast = self._get_broadcast_addr()
        vote_packet = json.dumps({
            "type":     "ELECTION_VOTE",
            "from_ip":  self.my_ip,
            "vote_for": my_vote
        }).encode("utf-8")

        deadline = time.time() + ELECTION_TIMEOUT
        while time.time() < deadline:
            for target in [bcast, "255.255.255.255"]:
                try:
                    bcast_sock.sendto(vote_packet, (target, ELECTION_PORT))
                except Exception:
                    pass
            with self.election_lock:
                known_ips  = set(peers.keys())
                voted_ips  = set(self.election_votes.keys()) - {self.my_ip}
            if known_ips and known_ips.issubset(voted_ips):
                print("[Election] All peers voted. Proceeding early.")
                break
            time.sleep(0.5)

        bcast_sock.close()

        with self.election_lock:
            votes = dict(self.election_votes)

        if not votes:
            return self.my_ip

        tally: dict = {}
        for _, candidate in votes.items():
            tally[candidate] = tally.get(candidate, 0) + 1

        max_votes     = max(tally.values())
        top_candidates = [ip for ip, v in tally.items() if v == max_votes]
        winner        = min(top_candidates, key=self._ip_key)

        print(f"[Election] Winner: {winner} (tally: {tally})")
        return winner

    def _election_listener(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", ELECTION_PORT))
        sock.settimeout(1.0)

        deadline = time.time() + ELECTION_TIMEOUT + 1
        while time.time() < deadline:
            try:
                data, (sender_ip, _) = sock.recvfrom(BUFFER_SIZE)
            except socket.timeout:
                continue
            except Exception:
                continue

            try:
                packet = json.loads(data.decode("utf-8"))
            except json.JSONDecodeError:
                continue

            if packet.get("type") == "ELECTION_VOTE":
                from_ip  = packet.get("from_ip", sender_ip)
                vote_for = packet.get("vote_for")
                if from_ip != self.my_ip and vote_for:
                    with self.election_lock:
                        self.election_votes[from_ip] = vote_for

        sock.close()

    # ─── Start ────────────────────────────────────────────────────────────

    def start(self, discovery_manager):
        """
        Entry point. Called once.
        Listens for an existing hub first — if found, connect directly.
        If not, run election and become hub or client based on result.
        """
        self.running          = True
        self._discovery_manager = discovery_manager   # store for re-elections
        self._enter_join_phase()

    def _enter_join_phase(self):
        """
        The core re-entrant logic. Called at startup AND after hub loss.
        Step 1: Listen for a HUB_ALIVE beacon.
        Step 2: If found, connect as client. If not, run election.

        This is what fixes Bug 1 (reconnect after hub restart) AND
        Bug 2 (late joiners find hub without running a second election).
        """
        # Reset state from any previous session
        self.is_hub    = False
        self.hub_ip    = None
        self.hub_socket = None
        with self.election_lock:
            self.election_votes = {}

        existing_hub = self._listen_for_hub()

        if existing_hub:
            # Hub already exists — skip election, connect directly
            self.hub_ip = existing_hub
            self.is_hub = False
            print(f"[Hub] Connecting to existing hub {self.hub_ip}")
            t = threading.Thread(
                target=self._run_client, daemon=True, name="ClientThread"
            )
            t.start()
        else:
            # No hub found — run election
            self.hub_ip = self._run_election(self._discovery_manager)
            self.is_hub = (self.hub_ip == self.my_ip)

            if self.is_hub:
                print(f"[Hub] I am the hub ({self.my_ip})")
                threading.Thread(
                    target=self._run_hub, daemon=True, name="HubThread"
                ).start()
                threading.Thread(
                    target=self._beacon_loop, daemon=True, name="BeaconThread"
                ).start()
            else:
                print(f"[Hub] I am a client. Connecting to hub {self.hub_ip}")
                threading.Thread(
                    target=self._run_client, daemon=True, name="ClientThread"
                ).start()

    def stop(self):
        self.running = False
        if self.hub_socket:
            try:
                self.hub_socket.close()
            except Exception:
                pass

    # ─── Hub Mode ─────────────────────────────────────────────────────────

    def _run_hub(self):
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(("", CHAT_PORT))
        server_sock.listen(50)      # up to 50 pending connections in queue
        server_sock.settimeout(1.0)

        print(f"[Hub] Listening for connections on port {CHAT_PORT}")

        while self.running and self.is_hub:
            try:
                client_sock, (client_ip, _) = server_sock.accept()
            except socket.timeout:
                continue
            except Exception as e:
                print(f"[Hub] Accept error: {e}")
                continue

            print(f"[Hub] Client connected: {client_ip}")
            with self.clients_lock:
                self.client_sockets[client_ip] = client_sock

            threading.Thread(
                target=self._handle_client,
                args=(client_sock, client_ip),
                daemon=True,
                name=f"ClientHandler-{client_ip}"
            ).start()

        server_sock.close()

    def _handle_client(self, sock: socket.socket, client_ip: str):
        while self.running:
            message = self._recv_message(sock)
            if message is None:
                break
            self._relay_to_all(message, exclude_ip=client_ip)
            if self.on_message:
                self.on_message(message.get("username", "?"), message.get("text", ""))

        print(f"[Hub] Client disconnected: {client_ip}")
        with self.clients_lock:
            self.client_sockets.pop(client_ip, None)
        sock.close()

    def _relay_to_all(self, message: dict, exclude_ip: str = None):
        with self.clients_lock:
            targets = list(self.client_sockets.items())
        for ip, sock in targets:
            if ip == exclude_ip:
                continue
            try:
                self._send_message(sock, message)
            except Exception as e:
                print(f"[Hub] Failed to relay to {ip}: {e}")

    # ─── Client Mode ──────────────────────────────────────────────────────

    def _run_client(self):
        """
        Connect to hub with retries. On success, read messages until
        connection drops. On drop, call _enter_join_phase() to reconnect
        or trigger a new election. This is what fixes Bug 1.
        """
        for attempt in range(20):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3)
                sock.connect((self.hub_ip, CHAT_PORT))
                sock.settimeout(None)
                self.hub_socket = sock
                print(f"[Client] Connected to hub {self.hub_ip}")
                break
            except (ConnectionRefusedError, socket.timeout, OSError):
                print(f"[Client] Hub not ready, retrying... ({attempt + 1}/20)")
                time.sleep(1)
        else:
            print("[Client] Could not connect to hub. Re-entering join phase.")
            if self.running:
                self._enter_join_phase()     # ← Bug 1 fix: don't give up
            return

        # Read loop
        self._read_from_hub()

        # Fell out of read loop — hub disconnected
        if self.running:
            print("[Client] Hub lost. Re-entering join phase...")
            time.sleep(1)                    # brief pause before re-joining
            self._enter_join_phase()         # ← Bug 1 fix: attempt reconnect

    def _read_from_hub(self):
        """Receive messages from hub. Returns when connection dies."""
        while self.running:
            message = self._recv_message(self.hub_socket)
            if message is None:
                print("[Client] Hub disconnected.")
                break
            if self.on_message:
                self.on_message(message.get("username", "?"), message.get("text", ""))

    def send_message(self, text: str):
        message = {"type": "CHAT", "username": self.username, "text": text}
        if self.is_hub:
            self._relay_to_all(message)
            if self.on_message:
                self.on_message(self.username, text)
        else:
            if self.hub_socket:
                try:
                    self._send_message(self.hub_socket, message)
                except Exception as e:
                    print(f"[Client] Failed to send: {e}")

    # ─── Wire Protocol ────────────────────────────────────────────────────

    def _send_message(self, sock: socket.socket, message: dict):
        payload = json.dumps(message).encode("utf-8")
        sock.sendall(len(payload).to_bytes(4, "big") + payload)

    def _recv_message(self, sock: socket.socket) -> dict | None:
        try:
            length_data = self._recv_exact(sock, 4)
            if length_data is None:
                return None
            payload = self._recv_exact(sock, int.from_bytes(length_data, "big"))
            if payload is None:
                return None
            return json.loads(payload.decode("utf-8"))
        except Exception:
            return None

    def _recv_exact(self, sock: socket.socket, n: int) -> bytes | None:
        data = b""
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                return None
            data += chunk
        return data
