from __future__ import annotations

from dataclasses import asdict

from PySide6.QtCore import Property, QAbstractListModel, QModelIndex, QObject, Qt, Signal, Slot

from ducai_studio.providers.tts import VieNeuTtsProvider, VoiceCatalog

_INVALID_INDEX = QModelIndex()


class VoiceCatalogModel(QAbstractListModel):
    countChanged = Signal()

    VoiceIdRole = Qt.ItemDataRole.UserRole + 1
    ProviderIdRole = Qt.ItemDataRole.UserRole + 2
    NameRole = Qt.ItemDataRole.UserRole + 3
    SourceRole = Qt.ItemDataRole.UserRole + 4
    DescriptionRole = Qt.ItemDataRole.UserRole + 5
    RightsLabelRole = Qt.ItemDataRole.UserRole + 6
    RightsStateRole = Qt.ItemDataRole.UserRole + 7
    InstalledRole = Qt.ItemDataRole.UserRole + 8
    RecommendedRole = Qt.ItemDataRole.UserRole + 9
    SummaryRole = Qt.ItemDataRole.UserRole + 10

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: list[dict] = []
        self.refresh()

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        values = {
            self.VoiceIdRole: item["voice_id"],
            self.ProviderIdRole: item["provider_id"],
            self.NameRole: item["name"],
            self.SourceRole: item["source"],
            self.DescriptionRole: item["description"],
            self.RightsLabelRole: item["rights_label"],
            self.RightsStateRole: item["rights_state"],
            self.InstalledRole: item["installed"],
            self.RecommendedRole: item["recommended"],
            self.SummaryRole: item["summary"],
            Qt.ItemDataRole.DisplayRole: item["name"],
        }
        return values.get(role)

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.VoiceIdRole: b"voiceId",
            self.ProviderIdRole: b"providerId",
            self.NameRole: b"voiceName",
            self.SourceRole: b"voiceSource",
            self.DescriptionRole: b"voiceDescription",
            self.RightsLabelRole: b"rightsLabel",
            self.RightsStateRole: b"rightsState",
            self.InstalledRole: b"installed",
            self.RecommendedRole: b"recommended",
            self.SummaryRole: b"summary",
        }

    @Property(int, notify=countChanged)
    def count(self) -> int:
        return len(self._items)

    @Slot(int, result="QVariantMap")
    def get(self, row: int) -> dict:
        return dict(self._items[row]) if 0 <= row < len(self._items) else {}

    @Slot(str, result="QVariantMap")
    def find(self, voice_id: str) -> dict:
        return next(
            (dict(item) for item in self._items if item["voice_id"] == voice_id),
            {},
        )

    def first_installed(self, provider_id: str = "") -> dict:
        return next(
            (
                dict(item)
                for item in self._items
                if item["installed"] and (not provider_id or item["provider_id"] == provider_id)
            ),
            {},
        )

    @Slot()
    def refresh(self) -> None:
        piper_items: list[dict] = []
        for entry in VoiceCatalog().entries():
            item = asdict(entry)
            item["rights_state"] = entry.rights_state.value
            state = "Đã cài" if entry.installed else "Chưa cài"
            item["summary"] = (
                f"{entry.name} • {entry.source} • {entry.description} • "
                f"{entry.rights_label} • {state}"
            )
            piper_items.append(item)
        vieneu = VieNeuTtsProvider()
        health = vieneu.health()
        profile = vieneu.profile_path
        clone_item = {
            "voice_id": profile.stem,
            "provider_id": vieneu.provider_id,
            "name": "Giọng clone của bạn",
            "source": "VieNeu 3.3 Turbo",
            "description": "Clone cục bộ chất lượng cao; chậm hơn Piper",
            "rights_label": "Chỉ dùng khi bạn có quyền với giọng mẫu",
            "rights_state": "permission_required",
            "installed": health.state.value == "ready",
            "recommended": False,
            "summary": health.summary,
            "profile_path": str(profile),
        }
        items = [clone_item, *piper_items]
        if items == self._items:
            return
        self.beginResetModel()
        self._items = items
        self.endResetModel()
        self.countChanged.emit()
