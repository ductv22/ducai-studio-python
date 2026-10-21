"""Real QML selection/deletion/scissors; isolated DB and no live user writes."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QSettings, QStandardPaths, Qt, QUrl
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtQml import QQmlApplicationEngine, QQmlExpression
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from shiboken6 import delete

from ducai_studio.domain.timeline_edit import build_scan_plan
from ducai_studio.infrastructure.project_repository import decode_project_snapshot
from ducai_studio.job_manager.service import JobManagerService
from ducai_studio.providers.subtitle_artwork import _ensure_fonts
from ducai_studio.ui.qt_runtime import configure_qt_plugin_dll_search
from ducai_studio.ui.subtitle_artwork_provider import SubtitleArtworkProvider
from ducai_studio.ui.timeline_media_provider import TimelineMediaProvider
from ducai_studio.ui.viewmodels.application_view_model import ApplicationViewModel


def items(item):
    yield item
    for child in item.childItems():
        yield from items(child)


def main() -> int:
    configure_qt_plugin_dll_search()
    qml = Path(os.environ.get('DUCAI_TEST_QML_ROOT') or (
        Path(__file__).resolve().parents[2]/'src/ducai_studio/ui/qml'))
    os.environ.setdefault('QT_QPA_FONTDIR', str(qml.parent/'assets/fonts'))
    os.environ.setdefault('QT_LOGGING_RULES',
                          'qt.multimedia.ffmpeg=false;qt.multimedia.ffmpeg.*=false')
    app = QApplication([])
    packaged_qml = os.environ.get('DUCAI_TEST_QML_ROOT')
    if packaged_qml:
        # A wheel fixture uses Python, not the frozen EXE's resource resolver.
        font_root = Path(packaged_qml).parent / 'assets/fonts'
        for font_path in sorted((*font_root.glob('*.ttf'), *font_root.glob('*.otf'))):
            assert QFontDatabase.addApplicationFont(str(font_path)) >= 0, font_path
    _ensure_fonts()
    QFont.insertSubstitution('Be Vietnam Pro', 'Noto Sans CJK SC')
    QStandardPaths.setTestModeEnabled(True)
    QQuickStyle.setStyle('Basic')
    directory = Path(sys.argv[1]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    source = Path(os.environ.get('DUCAI_TEST_SOURCE_VIDEO') or str(directory/'source.mp4'))
    if not source.is_file():
        subprocess.run([shutil.which('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
            'color=c=black:s=160x90:r=1', '-t', '60', '-c:v', 'libx264', '-threads', '1',
            '-y', str(source)], check=True, timeout=20,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    model = ApplicationViewModel(app)
    model.settings = QSettings(str(directory/'settings.ini'), QSettings.Format.IniFormat)
    model.runtime.start = lambda: None
    cues = [{'id': f'c{i}', 'index': i, 'text': f'字幕{i}', 'language': 'zh',
             'range': {'start_us': a, 'end_us': b}} for i, (a, b) in enumerate(
                 ((2_000_000, 2_300_000), (5_000_000, 7_000_000),
                  (6_000_000, 8_000_000), (22_000_000, 24_000_000)), 1)]
    project = {'id': 'gesture', 'name': 'Delete/Scissors', 'revision': 1,
        'source_cues': cues,
        'translated_cues': [{**c, 'text': f'Câu {c["index"]}', 'language': 'vi'} for c in cues],
        'media': [{'id': 'm', 'path': str(source), 'kind': 'video',
                   'duration_us': 60_000_000}],
        'tracks': [{'id': t, 'kind': k, 'name': t, 'clips': [
            {'id': t.lower(), 'track_id': t, 'media_id': 'm', 'link_group_id': 'av',
             'timeline_range': {'start_us': 0, 'end_us': 60_000_000},
             'source_range': {'start_us': 0, 'end_us': 60_000_000}}]}
            for t, k in (('V1', 'video'), ('A1', 'original_audio'))] + [
            {'id': 'S1', 'kind': 'subtitle', 'name': 'Sub', 'clips': [
                {'id': 's'+c['id'], 'track_id': 'S1', 'cue_id': c['id'],
                 'timeline_range': c['range']} for c in cues]}]}
    model.project_state.replace(project)
    service = JobManagerService(database_path=directory/'state.db',
                                artifact_root=directory/'artifacts')
    service.projects.save(project_path=directory/'fixture.ducai',
                          snapshot=decode_project_snapshot(project))
    requests = []
    deferred = os.environ.get('DUCAI_TEST_DEFERRED_EDITS') == '1'

    def request(action, params=None, context=None):
        if deferred and action in {'subtitle_command', 'timeline_command', 'effect_command'}:
            params = {**params, 'deferred': True}
        requests.append((action, params, context))

    model.runtime.request = request
    engine = QQmlApplicationEngine(model)
    engine.addImageProvider('subtitle-artwork', SubtitleArtworkProvider())
    engine.addImageProvider('timeline-media', TimelineMediaProvider(directory/'media-cache'))
    engine.addImportPath(str(qml))
    engine.setInitialProperties({'appModel': model})
    engine.load(QUrl.fromLocalFile(str(qml/'Main.qml')))
    try:
        window = engine.rootObjects()[0]
        assert window.property('font').family() == 'Be Vietnam Pro'
        button_color = QQmlExpression(engine.rootContext(), window,
                                     'palette.button.toString()').evaluate()
        assert button_color == ('#18232d', False), button_color
        root_dark = QQmlExpression(engine.rootContext(), window,
                                  'palette.dark.toString()').evaluate()
        assert root_dark == ('#25313c', False), root_dark
        window.setWidth(1992)
        window.setHeight(1284)
        window.show()
        model.navigate('timeline')
        app.processEvents()
        timeline = window.findChild(QObject, 'timelineScreen')
        timeline.setProperty('viewportEndUs', 60_000_000)
        QTest.qWait(100)
        mixer_labels = [item for item in items(window.contentItem())
                        if item.objectName().startswith('mixerTrackLabel-')]
        assert len(mixer_labels) == 3, [item.objectName() for item in mixer_labels]
        assert all(item.property('font').family() == 'Be Vietnam Pro'
                   for item in mixer_labels)
        background_combo = next(item for item in items(window.contentItem())
                                if item.objectName() == 'subtitleBackgroundMode')
        indicator_color = QQmlExpression(engine.rootContext(), background_combo,
                                        'palette.dark.toString()').evaluate()
        assert indicator_color == ('#a8b3be', False), indicator_color

        def locate(name):
            return next(i for i in items(window.contentItem())
                        if i.objectName() == name and i.isVisible())

        def click(item, button=Qt.MouseButton.LeftButton, modifiers=Qt.KeyboardModifier.NoModifier):
            point = item.mapToScene(QPointF(item.width()/2, item.height()/2)).toPoint()
            assert 0 < point.y() < window.height(), (item.objectName(), point)
            QTest.mouseClick(window, button, modifiers, point)
            app.processEvents()

        def type_text(value):
            for character in value:
                QTest.keyClick(window, Qt.Key(ord(character.upper())),
                               Qt.KeyboardModifier.ShiftModifier if character.isupper()
                               else Qt.KeyboardModifier.NoModifier)

        def ack():
            action, payload, context = requests[-1]
            if action == 'copy_video_clip':
                patch = service.copy_video_clip(**payload)
            elif action == 'subtitle_command':
                patch = service.subtitle_command(project_id=payload['project_id'],
                    command=payload['command'], cue_id=payload.get('cue_id'),
                    language=payload['language'], params=payload['values'], compact=True,
                    deferred=payload.get('deferred', False))
            elif action == 'save_project_edits':
                patch = service.save_project_edits(payload['project_id'])
            else:
                patch = service.timeline_command(**payload)
            model._action_succeeded(action, patch, context)
            # Large cut projections complete on the snapshot worker.
            for _ in range(100):
                app.processEvents()
                if not model.timelineBusy and not model.subtitleBusy:
                    break
                QTest.qWait(10)
            assert not model.timelineBusy and not model.subtitleBusy

        model.setPlayheadMs(1000)
        click(locate('timelineClipBody-sc1'))  # Eight-pixel cue, unrelated overlaps.
        assert model.selectedCueId == 'c1' and model.playheadUs == 1_000_000
        assert locate('subtitleTextToolbar').property('hasCue')
        assert timeline.property('inspectorTab') == 'text'
        assert not any('timelineCut' in i.objectName() for i in items(window.contentItem()))
        # Actual numeric input above the old cap, ACK and global keyboard history.
        size = locate('sizeSpin')
        click(size)
        QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        type_text('240')
        QTest.keyClick(window, Qt.Key.Key_Return)
        QTest.qWait(200)
        assert requests[-1][1]['values']['font_size'] == 240
        ack()
        assert service.projects.load('gesture').source_cues[0].text_font_size == 240
        timeline.forceActiveFocus()
        QTest.keyClick(window, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert requests[-1][1]['command'] == 'undo'
        ack()
        assert model.selectedCue['text_font_size'] == 12
        assert locate('sizeSpin').property('value') == 12
        QTest.keyClick(window, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
        ack()
        assert model.selectedCue['text_font_size'] == 240
        assert locate('sizeSpin').property('value') == 240
        # No hovering can submit an edit; the inspector stays readable at minimum size.
        count = len(requests)
        window.setWidth(1180)
        window.setHeight(720)
        QTest.qWait(120)
        for i in items(window.contentItem()):
            if i.isVisible() and i.property('hoverEnabled'):
                point = i.mapToScene(QPointF(i.width()/2, i.height()/2)).toPoint()
                if 0 <= point.x() < window.width() and 0 <= point.y() < window.height():
                    QTest.mouseMove(window, point)
        assert len(requests) == count
        window.grabWindow().save(str(directory/'text-inspector-1180.png'))
        window.setWidth(1992)
        window.setHeight(1284)
        QTest.qWait(100)
        # Switching cues while editing must persist to the original identity.
        field = locate('timelineCueTextField')
        click(field)
        QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        type_text('edited first cue')
        click(locate('timelineClipBody-sc2'))
        QTest.qWait(200)
        assert requests[-1][1]['cue_id'] == 'c1'
        ack()
        assert service.projects.load('gesture').translated_cues[0].text == 'edited first cue', (
            service.projects.load('gesture').translated_cues[0].text, requests[-1])
        assert model.selectedCueId == 'c2'
        click(locate('timelineClipBody-sc1'))
        click(locate('deleteTimelineCueButton'))
        assert requests[-1][1]['command'] == 'delete_cues'
        ack()
        assert not model.project_state.subtitle_cues.find('c1')
        assert not model.project_state.clip_info('sc1')

        click(locate('timelineClipBody-sc2'), Qt.MouseButton.RightButton)
        assert model.selectedCueId == 'c2'
        menu = next(i for i in items(window.contentItem())
                    if i.property('text') == 'Xóa cue chữ và nền' and i.isVisible())
        click(menu)
        assert requests[-1][1]['cue_id'] == 'c2'
        ack()
        assert not model.project_state.subtitle_cues.find('c2')

        click(locate('timelineClipBody-sc3'))
        QTest.keyClick(window, Qt.Key.Key_Delete)
        assert requests[-1][1]['command'] == 'delete_cues'
        ack()
        assert not model.project_state.subtitle_cues.find('c3')

        model.undoSubtitle()
        ack()
        click(locate('timelineClipBody-sc3'))
        click(locate('timelineClipBody-sc4'), modifiers=Qt.KeyboardModifier.ControlModifier)
        assert model.selectedCueCount == 2
        count = len(requests)
        click(locate('deleteTimelineCueButton'))
        assert len(requests) == count + 1
        assert set(requests[-1][1]['values']['cue_ids']) == {'c3', 'c4'}
        ack()
        assert model.project_state.subtitle_cues.count == 0
        model.undoSubtitle()
        ack()
        assert model.project_state.subtitle_cues.count == 2

        if deferred:
            assert service.projects.persisted_head_revision('gesture') == 1
            assert model.projectDirty
            # Ctrl+S must first commit the actual focused field, then persist its ACK.
            click(locate('timelineClipBody-sc4'))
            field = locate('timelineCueTextField')
            click(field)
            QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
            type_text('saved by control s')
            QTest.keyClick(window, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
            QTest.qWait(200)
            assert requests[-1][0] == 'subtitle_command'
            ack()
            QTest.qWait(20)
            assert requests[-1][0] == 'save_project_edits', requests[-1]
            ack()
            assert not model.projectDirty and not model.projectSaveBusy
            reopened = JobManagerService(database_path=directory/'state.db',
                                         artifact_root=directory/'artifacts')
            assert reopened.projects.load('gesture') == service.projects.load('gesture')
            assert service.projects.load('gesture').translated_cues[-1].text == 'saved by control s'

        click(locate('timelineClipBody-sc4'))
        field = locate('timelineCueTextField')
        click(field)
        original_text = str(field.property('originalText'))
        count = len(requests)
        QTest.keyClick(window, Qt.Key.Key_X)
        QTest.keyClick(window, Qt.Key.Key_Delete)
        QTest.keyClick(window, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(window, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
        assert len(requests) == count, 'typing shortcuts must not edit media/cues'
        # Restore through Qt key input so the editor's pending draft and text
        # stay aligned; assigning TextField.text bypasses its onTextEdited path.
        QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        app.clipboard().setText(original_text)
        QTest.keyClick(window, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        assert locate('subtitleTextToolbar').property('inputDraft') == original_text
        timeline.forceActiveFocus()
        model.setPlayheadMs(20_000)
        QTest.keyClick(window, Qt.Key.Key_X)
        assert requests[-1][1]['command'] == 'split_video', (
            requests[-1],
            field.property('activeFocus'),
            field.property('text'),
            field.property('originalText'),
            locate('subtitleTextToolbar').property('inputDraft'),
        )
        ack()
        model.setPlayheadMs(30_000)
        click(locate('timelineScissorsButton'))
        assert requests[-1][1]['command'] == 'split_video'
        ack()
        videos = model.project_state._video_clips
        assert len(videos) == 3
        middle = next(c for c in videos if c['timeline_range']['start_us'] == 20_000_000)
        click(locate('timelineClipBody-'+middle['id']))
        assert not model.selectedCueId
        QTest.keyClick(window, Qt.Key.Key_Delete)
        assert requests[-1][1]['command'] == 'delete_video_segment'
        ack()
        saved = service.projects.load('gesture')
        plan = build_scan_plan(saved)
        assert plan['duration_us'] == 50_000_000
        assert not model.project_state.subtitle_cues.find('c4')
        assert model.playheadUs == 20_000_000
        model.undoTimeline()
        ack()
        assert len(model.project_state._video_clips) == 3
        assert model.project_state.subtitle_cues.find('c4')
        model.redoTimeline()
        ack()
        if deferred:
            model.saveProject()
            ack()
        reopened = JobManagerService(database_path=directory/'state.db',
                                     artifact_root=directory/'artifacts')
        assert build_scan_plan(reopened.projects.load('gesture')) == plan
        # New video context actions use the real isolated service and ACK path.
        model.undoTimeline()
        ack()
        video = model.project_state._video_clips[0]
        click(locate('timelineClipBody-' + video['id']), Qt.MouseButton.RightButton)
        menu = window.findChild(QObject, 'videoClipMenu')
        assert menu.property('opened')
        labels = [item.property('text') for item in items(window.contentItem())
                  if item.isVisible() and item.property('text') in {
                      'Cắt    X', 'Xóa    Delete', 'Copy    Ctrl+C', 'Paste    Ctrl+V',
                      'Nhân bản    Ctrl+D', 'Tắt âm thanh'}]
        assert len(set(labels)) == 6, labels  # Button and its content Text expose the same label.
        QTest.keyClick(window, Qt.Key.Key_Escape)
        timeline.forceActiveFocus()
        QTest.keyClick(window, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        assert requests[-1][0] == 'copy_video_clip'
        ack()
        model.setPlayheadMs(1234)
        QTest.keyClick(window, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        assert requests[-1][1]['command'] == 'paste_video'
        ack()
        saved = service.projects.load('gesture')
        pasted = next(t for t in saved.tracks if t.id == 'V2').clips[0]
        assert pasted.timeline_range.start_us == 1_234_000
        assert model.project_state.previewDescriptor(2_000_000)['clipId'] == pasted.id
        model.selectClip(pasted.id)
        model.toggleSelectedVideoMuted()
        ack()
        assert model.selectedClipMuted
        model.toggleSelectedVideoMuted()
        ack()
        assert not model.selectedClipMuted
        timeline.forceActiveFocus()
        QTest.keyClick(window, Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier)
        assert requests[-1][1]['command'] == 'duplicate_video'
        ack()
        field = locate('previewExactTimeField')
        click(field)
        QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        type_text('00:00:02.345')
        QTest.keyClick(window, Qt.Key.Key_Return)
        assert model.playheadUs == 2_345_000
        count = len(requests)
        click(field)
        QTest.keyClick(window, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(window, Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier)
        assert len(requests) == count, 'time input must not invoke clip clipboard shortcuts'
        model.navigate('export')
        app.processEvents()
        model._export_job_id = 'export-state-contract'
        model._set_export_busy(True)
        model._set_export_job_state('queued')
        app.processEvents()
        primary = locate('exportPrimaryButton')
        assert primary.property('enabled') and primary.property('text') == 'Xuất video'
        click(primary)
        assert requests[-1][0] == 'start_job' and model.exportJobState == 'start_requested'
        assert not primary.property('enabled')
        model._set_export_job_state('running')
        app.processEvents()
        assert primary.property('text') == 'Tạm dừng'
        click(primary)
        assert requests[-1][0] == 'pause_job' and model.exportJobState == 'pause_requested'
        model._set_export_job_state('paused')
        app.processEvents()
        assert primary.property('text') == 'Tiếp tục'
        click(primary)
        assert requests[-1][0] == 'resume_job' and model.exportJobState == 'resume_requested'
        model._set_export_job_state('running')
        app.processEvents()
        click(locate('exportCancelButton'))
        assert requests[-1][0] == 'cancel_job' and model.exportJobState == 'cancel_requested'
        model._export_job_id = ''
        model._set_export_busy(False)
        print('PASS: toolbar/right-click/Delete, atomic group deletion, '
              'narrow overlapping cues, typing isolation, '
              'X/scissors/Delete middle, ripple, Undo/Redo/reopen, '
              'video clipboard/mute/duplicate, exact millisecond seek and export state controls')
        return 0
    finally:
        delete(engine)
        model.forceShutdown()


if __name__ == '__main__':
    raise SystemExit(main())
