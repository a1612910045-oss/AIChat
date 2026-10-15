"""ChatGPT account and account-specific model picker; network work stays off Qt."""
from __future__ import annotations

import threading
import webbrowser

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, ComboBox, PrimaryPushButton, PushButton

from app import settings
from core.chatgpt_auth import AuthError, USAGE_URL, auth


class _Finished(QObject):
    done = Signal(str, object, str)


class ChatGPTAccount(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("chatgptAccountCard")
        self._busy = False
        self._flow = None
        self._model_client = None
        self._model_slugs = []
        self._account_ids = []
        self._finished = _Finished(self)
        self._finished.done.connect(self._done)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(12)
        self.statusLabel = BodyLabel("未登录 ChatGPT")
        self.statusLabel.setWordWrap(True)
        self.statusLabel.setAccessibleName("ChatGPT 登录状态")
        box.addWidget(self.statusLabel)
        box.addWidget(BodyLabel("ChatGPT 账号"))
        self.accountBox = ComboBox()
        self.accountBox.setMinimumWidth(0)
        self.accountBox.setAccessibleName("选择 ChatGPT 账号")
        self.accountBox.currentIndexChanged.connect(self._select_account)
        box.addWidget(self.accountBox)
        self.signInButton = PrimaryPushButton("Continue with ChatGPT")
        self.signInButton.setAccessibleName("Continue with ChatGPT")
        self.signInButton.clicked.connect(lambda: self._login(False))
        box.addWidget(self.signInButton)
        row = QHBoxLayout()
        self.addButton = PushButton("添加账号")
        self.addButton.clicked.connect(lambda: self._login(True))
        self.signOutButton = PushButton("退出登录")
        self.signOutButton.clicked.connect(self._sign_out)
        row.addWidget(self.addButton)
        row.addWidget(self.signOutButton)
        box.addLayout(row)
        self.cancelButton = PushButton("取消登录")
        self.cancelButton.clicked.connect(self.cancel_login)
        self.cancelButton.hide()
        box.addWidget(self.cancelButton)
        box.addWidget(BodyLabel("GPT 模型 · 判断和起草共用"))
        self.modelBox = ComboBox()
        self.modelBox.setMinimumWidth(0)
        self.modelBox.setAccessibleName("判断和起草使用的 GPT 模型")
        self.modelBox.currentIndexChanged.connect(self._model_selected)
        box.addWidget(self.modelBox)
        row = QHBoxLayout()
        self.refreshButton = PushButton("刷新模型")
        self.refreshButton.clicked.connect(self._fetch_models)
        self.usageButton = PushButton("管理用量")
        self.usageButton.clicked.connect(lambda: webbrowser.open(USAGE_URL))
        row.addWidget(self.refreshButton)
        row.addWidget(self.usageButton)
        box.addLayout(row)
        self.hint = BodyLabel("在浏览器中登录并授权使用订阅额度。无需填写 API Key。")
        self.hint.setWordWrap(True)
        box.addWidget(self.hint)
        self.feedback = BodyLabel("")
        self.feedback.setWordWrap(True)
        self.feedback.setAccessibleName("ChatGPT 操作结果")
        box.addWidget(self.feedback)
        self.refresh()
        QTimer.singleShot(0, self._initial_models)

    def refresh(self):
        status = auth.status()
        self.accountBox.blockSignals(True)
        self.accountBox.clear()
        accounts = auth.accounts()
        self._account_ids = [p["client_id"] for p in accounts]
        self.accountBox.addItems([f"{p['email']} · {p['client_id'][-6:]}" for p in accounts])
        if status["client_id"] in self._account_ids:
            self.accountBox.setCurrentIndex(self._account_ids.index(status["client_id"]))
        self.accountBox.blockSignals(False)
        if status["connected"]:
            self.statusLabel.setText("已登录：" + status["email"] +
                                     (" · 订阅额度已授权" if status["enabled"] else " · 订阅额度未授权"))
        else:
            self.statusLabel.setText("未登录 ChatGPT")
        self.signInButton.setText("授权订阅额度" if status["connected"] and not status["enabled"] else "Continue with ChatGPT")
        if self._model_client != status["client_id"] or not status["enabled"]:
            self._model_slugs = []
            self.modelBox.blockSignals(True)
            self.modelBox.clear()
            self.modelBox.blockSignals(False)
            self._model_client = status["client_id"]
        self._update_buttons()

    def _update_buttons(self):
        status = auth.status()
        self.signInButton.setEnabled(not self._busy)
        self.addButton.setEnabled(not self._busy)
        self.accountBox.setEnabled(not self._busy and bool(self._account_ids))
        self.signOutButton.setEnabled(not self._busy and status["connected"])
        self.refreshButton.setEnabled(not self._busy and status["enabled"])
        self.modelBox.setEnabled(not self._busy and status["enabled"] and bool(self._model_slugs))

    def _work(self, kind, function):
        if self._busy:
            return
        self._busy = True
        self._update_buttons()

        def run():
            try:
                result, error = function(), ""
            except AuthError as exc:
                result, error = None, str(exc)
            except Exception:
                result, error = None, "操作未完成，请检查网络或重新登录。"
            try:
                self._finished.done.emit(kind, result, error)
            except RuntimeError:
                pass  # The app may have closed while browser authorization was pending.

        threading.Thread(target=run, daemon=True).start()

    def _login(self, new_account):
        if self._busy:
            return
        status = auth.status()
        try:
            pending = (self._flow.client_id if not new_account and self._flow and
                       not self._flow.profile and self._flow.client_id != "dynamic_agent_client" else None)
            self._flow = auth.new_login(None if new_account or pending else status["client_id"],
                                       consent=status["connected"] and not status["enabled"] and not new_account,
                                       pending_client_id=pending)
        except AuthError as error:
            self.feedback.setText(str(error))
            return
        self.feedback.setText("请在浏览器中完成 ChatGPT 登录及订阅授权，完成后回到这里。")
        self.cancelButton.show()
        self._work("login", self._flow.run)

    def cancel_login(self):
        if self._flow:
            self._flow.cancel()

    def _sign_out(self):
        if self._busy:
            return
        # Discard outstanding recommendations before revocation starts.
        self.changed.emit()
        self.feedback.setText("正在退出登录…")
        self._work("logout", auth.sign_out)

    def _select_account(self, index):
        if self._busy or not 0 <= index < len(self._account_ids):
            return
        try:
            auth.select(self._account_ids[index])
        except AuthError as error:
            self.feedback.setText(str(error))
            return
        self.refresh()
        self.changed.emit()
        self._initial_models()

    def _initial_models(self):
        if auth.status()["enabled"] and not self._busy:
            self._fetch_models()

    def _fetch_models(self):
        self.feedback.setText("正在获取此账号可用的 GPT 模型…")
        self._work("models", auth.list_models)

    def _model_selected(self, index):
        if not 0 <= index < len(self._model_slugs):
            return
        try:
            settings.save(model_text=self._model_slugs[index], model_client_id=self._model_client)
        except OSError:
            self.feedback.setText("模型选择保存失败，请检查程序目录是否可写。")
            return
        self.feedback.setText("此 GPT 模型将负责判断和起草。")
        self.changed.emit()

    def _done(self, kind, result, error):
        self._busy = False
        if kind == "login":
            self.cancelButton.hide()
            if not error:
                self._flow = None
        self.refresh()
        if error:
            self.feedback.setText(error)
            return
        if kind == "models":
            self._model_client = auth.status()["client_id"]
            selected = settings.gpt_model()
            self._model_slugs = [m["slug"] for m in result]
            self.modelBox.blockSignals(True)
            self.modelBox.clear()
            self.modelBox.addItems([m["display_name"] for m in result])
            index = self._model_slugs.index(selected) if selected in self._model_slugs else 0
            self.modelBox.setCurrentIndex(index)
            self.modelBox.blockSignals(False)
            self._model_selected(index)
            self._update_buttons()
        else:
            self.changed.emit()
            self.feedback.setText(result if kind == "logout" else
                                  ("登录完成，正在获取模型。" if result["enabled"] else
                                   "已登录，请点击“授权订阅额度”后再使用。"))
            if kind == "login" and result["enabled"]:
                self._fetch_models()
