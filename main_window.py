# main_window.py
# Stage 4: PyQt6 GUI — WhatsApp-style
#
# Entry point. Run this instead of main.py.
# Replicates the exact same flow as main.py:
#   1. Start discovery
#   2. Wait 4 seconds for peers
#   3. Run election + connect
#   4. Chat
#
# All networking runs in a background QThread.
# All UI updates happen on the main thread via Qt signals.

import sys
import time
import random
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QHBoxLayout, QVBoxLayout, QScrollArea,
    QLineEdit, QPushButton, QLabel, QFrame,
    QSizePolicy, QGraphicsDropShadowEffect
)
from PyQt6.QtCore import (
    Qt, pyqtSignal, QThread, QSize,
    QPropertyAnimation, QEasingCurve, QRect
)
from PyQt6.QtGui import (
    QFont, QColor, QPainter, QPainterPath,
    QBrush, QPen, QFontMetrics, QPixmap
)

from discovery import DiscoveryManager
from hub_manager import HubManager


# ─── Palette ──────────────────────────────────────────────────────────────────

BG_DEEP     = "#0a0f1a"      # outermost background
BG_SIDEBAR  = "#0e1420"      # left sidebar
BG_CHAT     = "#111827"      # chat area background
BG_BUBBLE_OUT = "#2563eb"    # our outgoing bubbles — vivid blue
BG_BUBBLE_IN  = "#1e2a3a"    # incoming bubbles — dark slate
BG_INPUT    = "#1a2235"      # input bar background
ACCENT      = "#3b82f6"      # blue accent
ACCENT2     = "#60a5fa"      # lighter blue
TEXT_PRIMARY   = "#f1f5f9"
TEXT_SECONDARY = "#64748b"
TEXT_TIMESTAMP = "#475569"
BORDER      = "#1e2d45"

# Per-user avatar colors — deterministic by username hash
AVATAR_COLORS = [
    "#ef4444", "#f97316", "#eab308", "#22c55e",
    "#06b6d4", "#8b5cf6", "#ec4899", "#14b8a6",
]


def avatar_color(username: str) -> str:
    return AVATAR_COLORS[hash(username) % len(AVATAR_COLORS)]


# ─── Avatar Widget ────────────────────────────────────────────────────────────

class AvatarWidget(QWidget):
    """
    Colored circle with the first letter of the username.
    Drawn manually in paintEvent — no images needed.
    """
    def __init__(self, username: str, size: int = 36, parent=None):
        super().__init__(parent)
        self.username = username
        self._size = size
        self.setFixedSize(size, size)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        color = QColor(avatar_color(self.username))
        p.setBrush(QBrush(color))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(0, 0, self._size, self._size)

        p.setPen(QColor("#ffffff"))
        font = QFont("JetBrains Mono", int(self._size * 0.38), QFont.Weight.Bold)
        p.setFont(font)
        initial = self.username[0].upper() if self.username else "?"
        p.drawText(QRect(0, 0, self._size, self._size),
                   Qt.AlignmentFlag.AlignCenter, initial)


# ─── Message Bubble ───────────────────────────────────────────────────────────

class BubbleWidget(QWidget):
    """
    A single chat message rendered as a rounded bubble.
    Outgoing (own=True): right-aligned, blue.
    Incoming (own=False): left-aligned, dark slate, with avatar + username.
    """

    def __init__(self, username: str, text: str, timestamp: str,
                 own: bool = False, parent=None):
        super().__init__(parent)
        self.username  = username
        self.text      = text
        self.timestamp = timestamp
        self.own       = own

        self._build()

    def _build(self):
        outer = QHBoxLayout(self)
        outer.setContentsMargins(12, 3, 12, 3)
        outer.setSpacing(8)

        if self.own:
            outer.addStretch()
            outer.addWidget(self._make_bubble())
        else:
            avatar = AvatarWidget(self.username, size=34)
            # Align avatar to top of bubble
            avatar_wrap = QVBoxLayout()
            avatar_wrap.setContentsMargins(0, 2, 0, 0)
            avatar_wrap.setSpacing(0)
            avatar_wrap.addWidget(avatar)
            avatar_wrap.addStretch()
            avatar_container = QWidget()
            avatar_container.setLayout(avatar_wrap)
            outer.addWidget(avatar_container)
            outer.addWidget(self._make_bubble())
            outer.addStretch()

    def _make_bubble(self) -> QWidget:
        bubble = QFrame()
        bubble.setObjectName("bubble_out" if self.own else "bubble_in")
        bubble.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Minimum)
        bubble.setMaximumWidth(420)

        layout = QVBoxLayout(bubble)
        layout.setContentsMargins(12, 8, 12, 6)
        layout.setSpacing(2)

        if not self.own:
            name_label = QLabel(self.username)
            name_label.setFont(QFont("JetBrains Mono", 9, QFont.Weight.Bold))
            name_label.setStyleSheet(f"color: {avatar_color(self.username)};")
            layout.addWidget(name_label)

        text_label = QLabel(self.text)
        text_label.setFont(QFont("Inter", 10))
        text_label.setWordWrap(True)
        text_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        text_label.setStyleSheet(f"color: {TEXT_PRIMARY};")
        layout.addWidget(text_label)

        time_label = QLabel(self.timestamp)
        time_label.setFont(QFont("Inter", 8))
        time_label.setStyleSheet(f"color: {TEXT_TIMESTAMP};")
        time_label.setAlignment(
            Qt.AlignmentFlag.AlignRight if self.own else Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(time_label)

        bg = BG_BUBBLE_OUT if self.own else BG_BUBBLE_IN
        radius = "14px 14px 4px 14px" if self.own else "14px 14px 14px 4px"
        bubble.setStyleSheet(f"""
            QFrame#{bubble.objectName()} {{
                background-color: {bg};
                border-radius: 0px;
            }}
        """)
        # Use custom painting for proper rounded corners with tail shape
        bubble.setProperty("bg_color", bg)
        bubble.setProperty("own", self.own)
        bubble.paintEvent = lambda e, b=bubble: self._paint_bubble(e, b)

        return bubble

    @staticmethod
    def _paint_bubble(event, widget):
        p = QPainter(widget)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        own = widget.property("own")
        color = QColor(widget.property("bg_color"))
        p.setBrush(QBrush(color))
        p.setPen(Qt.PenStyle.NoPen)
        r = widget.rect()
        radius = 14

        path = QPainterPath()
        if own:
            # Rounded except bottom-right (tail)
            path.moveTo(r.left() + radius, r.top())
            path.lineTo(r.right() - radius, r.top())
            path.quadTo(r.right(), r.top(), r.right(), r.top() + radius)
            path.lineTo(r.right(), r.bottom())          # sharp bottom-right
            path.lineTo(r.left() + radius, r.bottom())
            path.quadTo(r.left(), r.bottom(), r.left(), r.bottom() - radius)
            path.lineTo(r.left(), r.top() + radius)
            path.quadTo(r.left(), r.top(), r.left() + radius, r.top())
        else:
            # Rounded except bottom-left (tail)
            path.moveTo(r.left() + radius, r.top())
            path.lineTo(r.right() - radius, r.top())
            path.quadTo(r.right(), r.top(), r.right(), r.top() + radius)
            path.lineTo(r.right(), r.bottom() - radius)
            path.quadTo(r.right(), r.bottom(), r.right() - radius, r.bottom())
            path.lineTo(r.left(), r.bottom())           # sharp bottom-left
            path.lineTo(r.left(), r.top() + radius)
            path.quadTo(r.left(), r.top(), r.left() + radius, r.top())

        path.closeSubpath()
        p.drawPath(path)


# ─── System Message ───────────────────────────────────────────────────────────

class SystemMessage(QWidget):
    """Centered gray pill — used for join/leave/status events."""
    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.addStretch()

        label = QLabel(text)
        label.setFont(QFont("Inter", 8))
        label.setStyleSheet(f"""
            color: {TEXT_SECONDARY};
            background: #162032;
            border-radius: 10px;
            padding: 3px 12px;
        """)
        layout.addWidget(label)
        layout.addStretch()


# ─── Online User Row ──────────────────────────────────────────────────────────

class UserRow(QWidget):
    """A single row in the online users sidebar."""
    def __init__(self, username: str, is_self: bool = False, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(10)

        avatar = AvatarWidget(username, size=32)
        layout.addWidget(avatar)

        name = QLabel(username + (" (you)" if is_self else ""))
        name.setFont(QFont("Inter", 10))
        name.setStyleSheet(f"color: {TEXT_PRIMARY if not is_self else ACCENT2};")
        layout.addWidget(name)
        layout.addStretch()

        # Green presence dot
        dot = QLabel("●")
        dot.setFont(QFont("Inter", 8))
        dot.setStyleSheet("color: #22c55e;")
        layout.addWidget(dot)


# ─── Network Worker ───────────────────────────────────────────────────────────

class NetworkWorker(QThread):
    """
    Mirrors main.py exactly — runs in a background QThread:
      1. Start DiscoveryManager
      2. Sleep 4 seconds (same as main.py)
      3. Start HubManager (election + connect)

    All results communicated back via Qt signals → main thread.
    """
    status_changed  = pyqtSignal(str)
    message_arrived = pyqtSignal(str, str)      # username, text
    peer_joined     = pyqtSignal(str, str)      # ip, username
    peer_left       = pyqtSignal(str, str)      # ip, username
    hub_role        = pyqtSignal(str, str)      # role ("hub"/"client"), hub_ip

    def __init__(self, username: str):
        super().__init__()
        self.username = username
        self.dm: DiscoveryManager = None
        self.hm: HubManager       = None

    def run(self):
        # ── Step 1: Discovery (mirrors main.py line-for-line) ────────────
        self.status_changed.emit("Starting discovery...")
        self.dm = DiscoveryManager(
            username=self.username,
            on_peer_joined=lambda ip, name: self.peer_joined.emit(ip, name),
            on_peer_left=lambda ip, name:   self.peer_left.emit(ip, name),
        )
        self.dm.start()

        # ── Step 2: Wait (same 4s as main.py) ───────────────────────────
        for i in range(4, 0, -1):
            self.status_changed.emit(f"Looking for peers... ({i}s)")
            time.sleep(1)

        peers = self.dm.get_peers()
        count = len(peers)
        self.status_changed.emit(
            f"Found {count} peer{'s' if count != 1 else ''}. Running election..."
        )

        # ── Step 3: Hub election + connect ───────────────────────────────
        self.hm = HubManager(
            my_ip=self.dm.my_ip,
            username=self.username,
            on_message=lambda u, t: self.message_arrived.emit(u, t),
        )
        self.hm.start(self.dm)

        role   = "hub" if self.hm.is_hub else "client"
        hub_ip = self.hm.hub_ip or self.dm.my_ip
        self.hub_role.emit(role, hub_ip)
        self.status_changed.emit(
            f"{'Hub' if self.hm.is_hub else 'Connected'} · {self.dm.my_ip}"
        )

    def send(self, text: str):
        if self.hm:
            self.hm.send_message(text)

    def shutdown(self):
        if self.hm:
            self.hm.stop()
        if self.dm:
            self.dm.stop()


# ─── Main Window ──────────────────────────────────────────────────────────────

class ChatWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.username = f"anon_{random.randint(100, 999)}"
        self.peers: dict = {}       # ip -> username

        self._build_ui()
        self._start_networking()

    # ─── UI ───────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.setWindowTitle(f"LAN Chat — {self.username}")
        self.setMinimumSize(760, 540)
        self.resize(960, 640)
        self.setStyleSheet(f"background: {BG_DEEP}; color: {TEXT_PRIMARY};")

        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        root_layout.addWidget(self._build_sidebar())
        root_layout.addWidget(self._build_chat_area())

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setFixedWidth(220)
        sidebar.setStyleSheet(f"background: {BG_SIDEBAR}; border-right: 1px solid {BORDER};")
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        header = QWidget()
        header.setFixedHeight(56)
        header.setStyleSheet(f"background: {BG_SIDEBAR}; border-bottom: 1px solid {BORDER};")
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(16, 0, 16, 0)

        title = QLabel("LAN Chat")
        title.setFont(QFont("JetBrains Mono", 13, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {ACCENT2};")
        h_layout.addWidget(title)
        h_layout.addStretch()

        layout.addWidget(header)

        # Online label
        self.online_label = QLabel("  Online")
        self.online_label.setFont(QFont("Inter", 8))
        self.online_label.setStyleSheet(
            f"color: {TEXT_SECONDARY}; padding: 12px 16px 4px 16px;"
            f"letter-spacing: 1px;")
        layout.addWidget(self.online_label)

        # User list scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("border: none; background: transparent;")
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.user_list_widget = QWidget()
        self.user_list_widget.setStyleSheet("background: transparent;")
        self.user_list_layout = QVBoxLayout(self.user_list_widget)
        self.user_list_layout.setContentsMargins(0, 0, 0, 0)
        self.user_list_layout.setSpacing(0)
        self.user_list_layout.addStretch()

        scroll.setWidget(self.user_list_widget)
        layout.addWidget(scroll)

        # Status bar at bottom of sidebar
        self.status_label = QLabel("Starting...")
        self.status_label.setFont(QFont("Inter", 8))
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(
            f"color: {TEXT_SECONDARY}; padding: 8px 12px;"
            f"border-top: 1px solid {BORDER}; background: {BG_SIDEBAR};")
        layout.addWidget(self.status_label)

        return sidebar

    def _build_chat_area(self) -> QWidget:
        area = QWidget()
        area.setStyleSheet(f"background: {BG_CHAT};")
        layout = QVBoxLayout(area)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Top bar
        topbar = QWidget()
        topbar.setFixedHeight(56)
        topbar.setStyleSheet(
            f"background: {BG_CHAT}; border-bottom: 1px solid {BORDER};")
        tb_layout = QHBoxLayout(topbar)
        tb_layout.setContentsMargins(20, 0, 20, 0)

        group_label = QLabel("# group-chat")
        group_label.setFont(QFont("JetBrains Mono", 12, QFont.Weight.Bold))
        group_label.setStyleSheet(f"color: {TEXT_PRIMARY};")
        tb_layout.addWidget(group_label)
        tb_layout.addStretch()

        self.peer_count_label = QLabel("1 online")
        self.peer_count_label.setFont(QFont("Inter", 9))
        self.peer_count_label.setStyleSheet(f"color: {TEXT_SECONDARY};")
        tb_layout.addWidget(self.peer_count_label)

        layout.addWidget(topbar)

        # Messages scroll area
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet(f"""
            QScrollArea {{ border: none; background: {BG_CHAT}; }}
            QScrollBar:vertical {{
                background: {BG_CHAT}; width: 6px;
            }}
            QScrollBar::handle:vertical {{
                background: #2a3a55; border-radius: 3px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
        """)

        self.messages_widget = QWidget()
        self.messages_widget.setStyleSheet(f"background: {BG_CHAT};")
        self.messages_layout = QVBoxLayout(self.messages_widget)
        self.messages_layout.setContentsMargins(0, 12, 0, 12)
        self.messages_layout.setSpacing(2)
        self.messages_layout.addStretch()   # pushes messages to bottom initially

        self.scroll_area.setWidget(self.messages_widget)
        layout.addWidget(self.scroll_area)

        # Input bar
        input_bar = QWidget()
        input_bar.setFixedHeight(64)
        input_bar.setStyleSheet(
            f"background: {BG_INPUT}; border-top: 1px solid {BORDER};")
        ib_layout = QHBoxLayout(input_bar)
        ib_layout.setContentsMargins(16, 12, 16, 12)
        ib_layout.setSpacing(10)

        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText("Message #group-chat")
        self.input_field.setFont(QFont("Inter", 10))
        self.input_field.setFixedHeight(40)
        self.input_field.setStyleSheet(f"""
            QLineEdit {{
                background: #1e2d45;
                color: {TEXT_PRIMARY};
                border: 1px solid {BORDER};
                border-radius: 20px;
                padding: 0 16px;
            }}
            QLineEdit:focus {{
                border: 1px solid {ACCENT};
                outline: none;
            }}
        """)
        self.input_field.returnPressed.connect(self._send)
        ib_layout.addWidget(self.input_field)

        send_btn = QPushButton("↑")
        send_btn.setFixedSize(40, 40)
        send_btn.setFont(QFont("Inter", 14, QFont.Weight.Bold))
        send_btn.setStyleSheet(f"""
            QPushButton {{
                background: {ACCENT};
                color: white;
                border: none;
                border-radius: 20px;
            }}
            QPushButton:hover {{ background: {ACCENT2}; }}
            QPushButton:pressed {{ background: #1d4ed8; }}
        """)
        send_btn.clicked.connect(self._send)
        ib_layout.addWidget(send_btn)

        layout.addWidget(input_bar)
        return area

    # ─── Networking ───────────────────────────────────────────────────────

    def _start_networking(self):
        self.worker = NetworkWorker(self.username)
        self.worker.status_changed.connect(self.status_label.setText)
        self.worker.message_arrived.connect(self._on_message)
        self.worker.peer_joined.connect(self._on_peer_joined)
        self.worker.peer_left.connect(self._on_peer_left)
        self.worker.hub_role.connect(self._on_hub_role)
        self.worker.start()

        # Add ourselves to sidebar immediately
        self._add_user_row("me", self.username, is_self=True)
        self._append_system(f"You joined as {self.username}")

    # ─── Slots ────────────────────────────────────────────────────────────

    def _on_message(self, username: str, text: str):
        self._append_bubble(username, text, own=False)

    def _on_peer_joined(self, ip: str, username: str):
        self.peers[ip] = username
        self._append_system(f"{username} joined")
        self._add_user_row(ip, username)
        self._update_counts()

    def _on_peer_left(self, ip: str, username: str):
        self.peers.pop(ip, None)
        self._append_system(f"{username} left")
        self._remove_user_row(ip)
        self._update_counts()

    def _on_hub_role(self, role: str, hub_ip: str):
        role_text = "You are the hub" if role == "hub" else f"Hub: {hub_ip}"
        self._append_system(role_text)

    # ─── Send ──────────────────────────────────────────────────────────────

    def _send(self):
        text = self.input_field.text().strip()
        if not text:
            return
        self.input_field.clear()
        self._append_bubble(self.username, text, own=True)
        self.worker.send(text)

    # ─── Chat helpers ──────────────────────────────────────────────────────

    def _append_bubble(self, username: str, text: str, own: bool):
        ts = time.strftime("%H:%M")
        bubble = BubbleWidget(username, text, ts, own=own)
        # Insert before the trailing stretch
        count = self.messages_layout.count()
        self.messages_layout.insertWidget(count - 1, bubble)
        self._scroll_to_bottom()

    def _append_system(self, text: str):
        msg = SystemMessage(text)
        count = self.messages_layout.count()
        self.messages_layout.insertWidget(count - 1, msg)
        self._scroll_to_bottom()

    def _scroll_to_bottom(self):
        # Use a zero-delay timer so Qt processes the layout before scrolling
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(0, lambda: self.scroll_area.verticalScrollBar().setValue(
            self.scroll_area.verticalScrollBar().maximum()
        ))

    # ─── User list helpers ────────────────────────────────────────────────

    def _add_user_row(self, key: str, username: str, is_self: bool = False):
        row = UserRow(username, is_self=is_self)
        row.setProperty("user_key", key)
        # Insert before stretch at end
        count = self.user_list_layout.count()
        self.user_list_layout.insertWidget(count - 1, row)
        self._update_counts()

    def _remove_user_row(self, key: str):
        layout = self.user_list_layout
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item and item.widget():
                w = item.widget()
                if w.property("user_key") == key:
                    layout.takeAt(i)
                    w.deleteLater()
                    break
        self._update_counts()

    def _update_counts(self):
        total = len(self.peers) + 1   # +1 for ourselves
        self.online_label.setText(f"  Online — {total}")
        self.peer_count_label.setText(f"{total} online")

    # ─── Cleanup ──────────────────────────────────────────────────────────

    def closeEvent(self, event):
        self.worker.shutdown()
        event.accept()


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = ChatWindow()
    window.show()
    sys.exit(app.exec())
