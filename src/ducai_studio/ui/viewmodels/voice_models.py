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
    PreviewSourceRole = Qt.ItemDataRole.UserRole + 11
    PreviewTextRole = Qt.ItemDataRole.UserRole + 12
    PreviewAvailableRole = Qt.ItemDataRole.UserRole + 13
    InstallableRole = Qt.ItemDataRole.UserRole + 14
    InstallStateRole = Qt.ItemDataRole.UserRole + 15
    InstallProgressRole = Qt.ItemDataRole.UserRole + 16
    InstallErrorRole = Qt.ItemDataRole.UserRole + 17
    DownloadBytesRole = Qt.ItemDataRole.UserRole + 18
    PresetNameRole = Qt.ItemDataRole.UserRole + 19
    CloneRole = Qt.ItemDataRole.UserRole + 20

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: list[dict] = []
        self._install_states: dict[str, tuple[str, int, str]] = {}
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
            self.PreviewSourceRole: item["preview_source"],
            self.PreviewTextRole: item["preview_text"],
            self.PreviewAvailableRole: item["preview_available"],
            self.InstallableRole: item["installable"],
            self.InstallStateRole: item["install_state"],
            self.InstallProgressRole: item["install_progress"],
            self.InstallErrorRole: item["install_error"],
            self.DownloadBytesRole: item["download_bytes"],
            self.PresetNameRole: item["preset_name"],
            self.CloneRole: item["is_clone"],
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
            self.PreviewSourceRole: b"previewSource",
            self.PreviewTextRole: b"previewText",
            self.PreviewAvailableRole: b"previewAvailable",
            self.InstallableRole: b"installable",
            self.InstallStateRole: b"installState",
            self.InstallProgressRole: b"installProgress",
            self.InstallErrorRole: b"installError",
            self.DownloadBytesRole: b"downloadBytes",
            self.PresetNameRole: b"presetName",
            self.CloneRole: b"isClone",
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
        catalog = VoiceCatalog()
        piper_items: list[dict] = []
        for entry in catalog.entries():
            if not entry.preview_available and not entry.installable:
                continue
            item = asdict(entry)
            item["rights_state"] = entry.rights_state.value
            item["preview_source"] = entry.preview_path
            item["preview_available"] = entry.preview_available
            item["download_bytes"] = entry.download_bytes
            item["is_clone"] = False
            state, progress, error = self._install_states.get(
                entry.voice_id,
                (
                    "installed" if entry.installed else "available",
                    100 if entry.installed else 0,
                    "",
                ),
            )
            if entry.installed:
                state, progress, error = "installed", 100, ""
                self._install_states.pop(entry.voice_id, None)
            item["install_state"] = state
            item["install_progress"] = progress
            item["install_error"] = error
            state = "Đã cài" if entry.installed else "Chưa cài"
            item["summary"] = (
                f"{entry.name} • {entry.source} • {entry.description} • "
                f"{entry.rights_label} • {state}"
            )
            piper_items.append(item)
        vieneu = VieNeuTtsProvider()
        profile = vieneu.profile_path
        vieneu_ready = catalog.vieneu_runtime_installed()
        preset_items: list[dict] = []
        for entry in catalog.vieneu_presets(installed=vieneu_ready):
            item = asdict(entry)
            item["rights_state"] = entry.rights_state.value
            item["preview_source"] = entry.preview_path
            item["preview_available"] = entry.preview_available
            item["download_bytes"] = 0
            item["install_state"] = "installed" if entry.installed else "runtime_missing"
            item["install_progress"] = 100 if entry.installed else 0
            item["install_error"] = ""
            item["is_clone"] = False
            state = "Đã cài" if entry.installed else "Thiếu runtime VieNeu"
            item["summary"] = (
                f"{entry.name} • {entry.source} • {entry.description} • "
                f"{entry.rights_label} • {state}"
            )
            preset_items.append(item)
        profile_ready = profile.is_file() and profile.with_suffix(".json").is_file()
        if vieneu_ready and profile_ready:
            clone_summary = "VieNeu 3 Turbo 3.8.1 đã cài và có profile clone cục bộ"
        elif vieneu_ready:
            clone_summary = "VieNeu 3 Turbo 3.8.1 đã cài; hãy chọn file mẫu để tạo giọng clone"
        else:
            clone_summary = "Thiếu hoặc hỏng runtime VieNeu 3.8.1"
        clone_item = {
            "voice_id": profile.stem,
            "provider_id": vieneu.provider_id,
            "name": "Giọng clone của bạn",
            "source": "VieNeu-TTS v3 Turbo 3.8.1",
            "description": "Clone cục bộ chất lượng cao; CUDA Graph tăng tốc theo batch",
            "rights_label": "Chỉ dùng khi bạn có quyền với giọng mẫu",
            "rights_state": "permission_required",
            "installed": vieneu_ready,
            "recommended": False,
            "summary": clone_summary,
            "profile_path": str(profile),
            "preset_name": "",
            "preview_source": "",
            "preview_text": "",
            "preview_available": False,
            "installable": False,
            "install_state": "installed" if vieneu_ready else "runtime_missing",
            "install_progress": 100 if vieneu_ready else 0,
            "install_error": "",
            "download_bytes": 0,
            "download_files": (),
            "is_clone": True,
        }
        items = [clone_item, *preset_items, *piper_items]
        if items == self._items:
            return
        self.beginResetModel()
        self._items = items
        self.endResetModel()
        self.countChanged.emit()

    def set_install_state(
        self,
        voice_id: str,
        state: str,
        progress: int = 0,
        error: str = "",
    ) -> None:
        normalized_progress = max(0, min(100, int(progress)))
        self._install_states[voice_id] = (state, normalized_progress, error)
        for row, item in enumerate(self._items):
            if item["voice_id"] != voice_id:
                continue
            item["install_state"] = state
            item["install_progress"] = normalized_progress
            item["install_error"] = error
            index = self.index(row, 0)
            self.dataChanged.emit(
                index,
                index,
                [self.InstallStateRole, self.InstallProgressRole, self.InstallErrorRole],
            )
            return
