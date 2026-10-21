# DUCAI STUDIO 2 architecture

## Overview

The rebuild separates interactive editing from long-running media engines. QML
renders state and dispatches actions. Pure domain objects enforce time, cue,
timeline and job rules. A dedicated Job Manager owns durable state and worker
processes.

## Dependency direction

```text
QML -> Qt view models -> application commands -> domain contracts
                         |-> persistence ports
                         |-> provider ports
                         `-> job manager client

infrastructure -> implements persistence, IPC and process ports
providers      -> implement OCR, translation, TTS, preview and export ports
```

Domain and application packages must not import PySide6, SQLite, OCR/TTS
frameworks, Playwright or FFmpeg helpers.

## Timebase

All persisted and compared timeline values use signed 64-bit integer
microseconds. Playback speed is a positive rational. Seconds and floating-point
values exist only at provider edges such as FFmpeg, libmpv and Qt Multimedia.

### Retained footage and cut joins

Cuts are non-destructive project edits, not rewritten media. Optional
`timeline_edit` retains canonical intervals; absence preserves legacy snapshots
without a database migration. The timing authority is
[`domain/timeline_edit.py`](../src/ducai_studio/domain/timeline_edit.py): it composes
source clips/speed, voice conform and retained intervals. Removed times have no
mapping, rather than clamping into a retained frame. Active cue collections are
filtered/reindexed for translation/TTS integrity; prior immutable revisions and
one-command Undo preserve the original data. Source files and cache stay intact.

[`JobManagerService.timeline_command`](../src/ducai_studio/job_manager/service.py)
owns atomic cuts and blocks unfinished jobs, including paused jobs. Compact
patches preserve fragment/track identities, cue deltas and changed overlay/conform
state. UI cut projections run on the snapshot worker; stale or uncertain responses
reconcile the durable head before further edits. A later free media edit
materializes the retained view within the same undoable transaction, avoiding
double application of the map.

Scissors use `split_video` to resolve the video at display playhead independently
of cue selection; conformed/retained footage materializes inside that one
transaction. `delete_video_segment` uses the synchronized retained-time cut,
rather than deleting an A/V pair and leaving other tracks behind. The legacy
range-cut API remains readable for older projects and synchronized internal cuts;
all retention controls, A/B markers and hover previews are absent from the UI.
Subtitle deletion removes stable source/translation pairs atomically: a shared
cue ID takes precedence over a legacy sparse index. Unrelated existing overlaps
cannot block deletion, since removing a cue cannot create a timing overlap.

Clip/subtitle/style/effect edits share one ordered in-memory session history.
The manager synchronizes both editor stacks only after a successful immutable
save; a failed save restores the previous stack. External job imports change
the head and invalidate stale editor histories. Undo/redo return the general
timeline patch (also for subtitle undo), including changed media, overlays,
tracks and cue indices; the UI does not apply a subtitle-only patch to a restored
video edit. Keyboard shortcuts flush pending subtitle writes and leave focused
text-input undo/redo to Qt. The Text inspector replaces the Timeline typography
row and advanced popup with direct scrollable property groups.

Source SRT replacement rebuilds the complete generated subtitle track set before
dropping obsolete subtitle assets; keeping old S2… clips while replacing only S1
would create dangling media references. The OCR publication merge subsequently
restores manual cues and rebuilds their tracks. D1 replacement/removal retains
assets still referenced by another track or overlay; backing files/cache are
never deleted by these snapshot mutations.

OCR freezes a scan plan at job creation. All frame passes read only retained
source intervals directly; evidence is isolated at cut joins. Publication maps
final SRT time back to canonical cue storage once. Plan/source fingerprints prevent
adopting full-film checkpoints into a cut project. The cut command, scan-plan and
mapping invariants are exercised in
[`test_timeline_cut_service.py`](../tests/integration/test_timeline_cut_service.py)
and [`test_timeline_edit.py`](../tests/timeline/test_timeline_edit.py).
Both final and review imports accept the recursively frozen JobSpec `Mapping`:
thaw at the scan-plan equality/time-map boundary, then map Timeline to canonical
time once. Reindexed subtitle clips have no media-relative source range; canonical
cue timestamps are not offsets into the shorter SRT asset. Stale scan layouts
are rejected before importing either result.

## Jobs and checkpoints

The Job Manager is the only SQLite writer. SQLite runs in WAL mode with short
transactions. Worker events are idempotent by event ID. A checkpoint becomes
visible only after its artifact is closed, hashed, atomically placed inside the
managed artifact root and committed with its fingerprint.

OCR, translation, TTS and export workers stream checkpoint SHA-256 reads in
bounded blocks; staging copies, verification, publication order and checkpoint
cadence are unchanged. Compact subtitle patches reuse immutable unchanged cues
and compare existing indexes directly before constructing an index-normalized
comparison. Index-only deltas remain separate from content/style upserts.

Runtime-diagnostic IPC requests run fixed read-only probes in a separate hidden,
launch-gated process tree, not on the manager's event loop. Admission permits one
report at a time; disconnect discards its response, and shutdown interrupts and
reaps the owned diagnostic tree. The complete report has a 300-second bound;
failure/timeout reports an incomplete diagnostic, never a fabricated CUDA failure.
Each report starts a fresh process, so process-local voice-health probe caches
are not shared between separate diagnostic requests. The report schema and
individual runtime checks are unchanged.

High-frequency progress uses a compact append path and a one-second producer
cadence. Job-list polling reads compact activity summaries and never transports
the full provider configuration or cue array; terminal, checkpoint and control
events retain the durable general event path.

Full job reads still query durable state each time, but may reuse the most recent
validated immutable `JobSpec` when all persisted input fields and the exact
configuration JSON match. Lifecycle timestamps, state and errors always come
from a fresh row. Changed credentials/options invalidate reuse even if the resume
fingerprint did not change. Retention is bounded to one spec, cleared for terminal
or missing jobs; compact history records cannot replace a worker's complete input.

Compact translation activity/cache records retain preset/custom-rule identity,
endpoint and credential references without transporting source cue arrays. Resume
matching includes this identity. A changed UI draft is durably acknowledged before
the safe stop/new-job route executes; failed saves do not resume stale settings.
Cache-list reads project only this bounded configuration in SQLite and reuse the
same job identity for checkpoint verification. Selecting a preview still validates
its full input once. Invalid historical configuration cannot hide valid caches.
An active translation owns automatic preview/cache selection; late replies from
older jobs cannot overwrite its rows or QA. Explicit historical selection remains
available when the current translation is not busy.
Only HTTP providers run one pending request on a daemon thread while the worker
handles controls/checkpoints. Paused results remain buffered until resume;
cancelled workers discard them. Browser/CLI providers retain their thread affinity.
Supervisor stop returns to the Qt event loop without waiting for pipe delivery;
its grace/kill callbacks bind the exact worker handle, not merely a reusable ID.

Freshly decoded project IPC payloads transfer ownership into the UI's canonical
snapshot, avoiding a redundant GUI-thread deepcopy. Regular model callers still
receive defensive isolation. Preview/edit projections remain separate from the
canonical snapshot. Successful media import applies the snapshot before opening
Timeline and does not issue a second project reload.

TTS cache scanning emits its first/last count and at most one intermediate
snapshot per second. WorkerSupervisor keeps only the newest pending progress
snapshot per job on a 200 ms timer, flushing it before checkpoint/control/terminal
events and worker exit. The manager resolves project ownership with a compact
query instead of decoding the entire job/cue configuration; Multi publishes each
managed progress change once. Only progress telemetry is coalesced, never durable
operations or cache contents.

Managed VieNeu jobs may exclusively lease one manager-owned warm engine over
authenticated loopback JSON. Launch parameters come from the manager, not socket
requests. One-use session tokens exist only in worker environment/memory; they
are not stored in job specs or SQLite. Loading runs on the broker thread, not
the Qt thread. Runtime/host/package identity and batch capacity must match for
reuse, and each new session clears voice state before binding its own profile.
The engine remains idle for at most 120 seconds after successful worker exit,
is evicted before a new OCR job, and is discarded on failure/cancel or manager
shutdown. Concurrent TTS jobs use the standalone path rather than sharing mutable
voice state. A pre-owned gate launcher keeps the host tree cancellable; its gate
read consumes exact bytes without stealing the child's first command. Custom
hosts, Piper and cache-only conform keep their existing behavior. No schema or
cache-fingerprint change is needed.

The view model keeps a marker for each current-project terminal job while
polling. A transition to succeeded, cancelled, interrupted or failed is
announced once with the stage name, a bounded/redacted worker reason translated
to Vietnamese and recovery guidance. Stable error identifiers remain persisted
for diagnostics, but are never shown as raw codes in the user interface.
Technical blockers such as missing/invalid WAVs are reported as QA stops rather
than generic worker failures. Semantic and cue-timing findings from a new
non-blocking TTS job are published as warnings and remain editable in Timeline;
interrupted or failed jobs explicitly state that
available cache/checkpoints remain usable. IPC failures use the same formatter
and auxiliary translation preview/QA polling failures are surfaced instead of
being silently discarded. The top-center toast is dismissible and remains
visible for up to 10 seconds for terminal errors and completions; progress
chatter stays in the footer to avoid replacing a readable terminal notice.

Intentional application exit requests safe checkpoint/stop and does not leave a
hidden process tree. Jobs still running after a crash become `interrupted`; the
next launch offers resume only when input, provider, model and settings
fingerprints still match.

The GUI and Job Manager each take an OS-visible lock so only one manager writes
the state database. The GUI also owns the manager through a Windows Job Object;
an abnormal GUI exit therefore cannot leave an unreachable manager or worker
tree behind.

## Offline licensing trust boundary

a140 defaults to the 128-character `DUCAI-L2.` permanent, machine-bound offline
key while continuing to verify the a139 `DUCAI-L1.` format without reactivation;
`DUCAI-L3.` adds the same machine binding with a signed absolute UTC expiry for
time-limited tests. L2 and L3 reconstruct the complete signed license metadata
before Ed25519 verification. The customer application contains only a pinned
licensing public key, distinct from the updater key: it contains neither a
signing secret nor a shortened signature. The signing vault, public trust root
and encrypted owner backup remain the same. Owner operations, explicit a139
legacy issuance, and customer-package exclusions are owned by
[`scripts/license-issuer.py`](../scripts/license-issuer.py),
[`scripts/build-license-issuer.ps1`](../scripts/build-license-issuer.ps1), and
[`scripts/check-license-release.py`](../scripts/check-license-release.py).

The Job Manager is the authoritative admission boundary; UI state is only a
presentation of its verified status. Direct worker routes also fail closed.
Locked mode preserves Settings and narrowly scoped recovery/cleanup operations,
whose executable allowlist is owned by
[`infrastructure/licensing_state.py`](../src/ducai_studio/infrastructure/licensing_state.py).
Token verification and identity derivation are owned by
[`infrastructure/offline_license.py`](../src/ducai_studio/infrastructure/offline_license.py)
and [`infrastructure/machine_identity.py`](../src/ducai_studio/infrastructure/machine_identity.py).

The identity combines the Windows installation ID with a usable SMBIOS UUID,
falling back to the system-disk manufacturer serial only when firmware identity
is absent or generic. Raw identifiers remain inside that adapter. Reinstallation
or replacement of the identity-bearing hardware can therefore require reissue.
There is deliberately no online revocation, automatic old-machine deactivation,
or anti-patch guarantee.

## Signed application update boundary

The source repository remains private. Update discovery anonymously queries the
public binary-only `ductv22/ducai-studio-releases` release feed and selects the
highest eligible PEP 440 version for the chosen stable or testing channel. The
application pins one Ed25519 public key. A canonical signed manifest binds the
product, channel, platform, archive size/hash and the exact sorted managed-file
list with per-file size/hash. GitHub asset metadata is only a cross-check; it is
not the trust root. Archive extraction rejects extra/missing entries, traversal,
ADS/reserved Windows names, links/reparse entries, source/wheel payloads and
expansion abuse. External engine hosts ship as interpreter-matched `.pyc`, so
the public update never carries private Python source.

Update staging is outside the install tree; portable builds use LocalAppData
rather than their adjacent data directory. Before shutdown, Job Manager grants
one global install reservation only when every job is terminal and Multi is not
running or pausing. All create/start/retry/resume and Multi admission paths honor
that reservation. The UI never auto-installs after work completes: eligibility
only returns the downloaded update to the explicit confirmation state.

An out-of-process PowerShell helper receives the digest of the already verified
manifest, rechecks it and every staged file, rejects reparse parents, copies into
a temporary target, verifies the temporary hash and only then replaces the
managed path. Its required Utility module is imported from that interpreter's
`PSHOME`, so an inherited PowerShell 7 module search path cannot break hashing
when the helper runs in Windows PowerShell 5.1. It does not trust the legacy unsigned release manifest and does
not delete stale a117 files. The helper publishes whole journal records by
atomic sibling-file replacement; transient Windows sharing/lock violations
receive a bounded retry, while other I/O errors still fail the install. Readers
cannot see truncated journal JSON or force rollback with a brief read handle.
The new process must atomically acknowledge a
nonce-bound health record containing its launched PID, version, install root and
backend-ready state. Failure kills only that launched process tree, restores the
backups in reverse order and reopens the old executable. User data and external
runtimes are outside the signed managed-file set. a118 is the initial bootstrap
boundary: older builds cannot initiate this protocol themselves. From a151,
install discovery uses the same existing runtime-program resolver as worker
launch: Nuitka standalone can expose a nonexistent `python.exe` alias through
`sys.executable`, while `sys.argv[0]` identifies the actual application. Discovery
still rejects source interpreters, arbitrary names and nonexistent binaries.
Affected packaged a148–a150 installations require one application-only manual
overlay, because their GUI fails before handing control to the installer.

Frozen-GUI smoke tests may seed a genuinely signed, extracted candidate using
`DUCAI_SMOKE_UPDATE_CANDIDATE`, only alongside explicit isolated app data and a
bounded smoke exit. This bypasses network discovery only, not install discovery,
Job Manager reservation, payload verification, the confirmation button, shutdown,
helper or health/rollback. Normal launches neither seed nor install a candidate.

Starting, pausing, resuming and cancelling a job are manager commands. The
manager commits a `start_requested` intent before spawning anything; `running`
is committed only after the worker echoes the correlated `started`
acknowledgement. Pause, resume and cancel likewise become durable outcomes only
after the worker acknowledgement. Each acknowledgement and state transition
shares one SQLite transaction. Checkpoint messages and their event IDs share a
transaction and carry a resume fingerprint covering input, provider version,
model and settings. Artifact-bearing checkpoints must originate in the managed
staging directory; the manager snapshots, hashes and re-verifies them in
immutable storage before exposing the database record.

Startup acknowledgement has a separate control deadline from pause/cancel. A
provider worker can spend tens of seconds importing its runtime before it reads
stdin; while its owned process is alive, the manager retries the correlated
`start` command idempotently but never converts a delayed ACK into a synthetic
`stop`. A delayed startup is recorded for diagnosis without consuming a Multi
provider retry.

Every start, pause, resume or active cancel first commits a `control_requested` event.
That event ID is the durable command ID sent to the worker. Providers must treat
repeated command IDs idempotently and echo `command_id` in `started`, `paused`,
`resumed` or cancel-related `stopped` acknowledgements. This closes both failure
windows: a database failure cannot occur after an unrecorded command, and a lost
ACK can be retried without applying the control twice.

The manager does not replay stale control commands after a restart. Its Windows
Job Object guarantees the old worker process tree has ended, so startup records
the unresolved command as `control_abandoned`, changes the job to
`interrupted`, and exposes retry of the whole job from the last fingerprinted
checkpoint. Replaying pause or cancel against a newly spawned worker would apply
the old intent to the wrong process.

Worker startup uses a gate launcher that is placed in the Windows Job Object
before it can spawn the provider process. Even a provider that immediately
starts FFmpeg or another child cannot escape process-tree ownership. Worker
stderr is bounded in memory, but its raw text is not written to durable storage;
SQLite keeps only character/line metadata. Only protocol failure or process exit
changes job lifecycle state.

Normal application close is asynchronous: the window waits while the manager
requests cooperative worker checkpoints. A bounded deadline still force-kills
the owned process tree if a provider stops responding, without blocking the GUI
thread.

History deletion is also a Job Manager command. Only terminal OCR/translation
records may be removed from the current project's history; active records are
rejected, verified staging/immutable artifacts are cleaned first, and the
database row is deleted only after cleanup succeeds. The GUI refreshes its
project-filtered models after the durable response, so a failed cleanup cannot
look like a successful deletion. Project deletion exposes an immediate busy
state in the view model while the same manager guard runs. Promoted checkpoint
files remain read-only during normal use. Deletion clears that attribute only
inside the already verified job-owned path before removal, which preserves the
immutability boundary without making Windows cleanup fail with access denied.
When the same `(job, stage, checkpoint key)` is replaced, the previous
unreferenced `technical` artifact row is retired after the new transaction
commits and its managed file is removed. Validated/final artifacts and artifacts
referenced by another checkpoint are never collected. OCR/TTS startup and every
artifact promotion also reserve free space up front, so a full disk produces a
Vietnamese recoverable error before another large immutable copy is attempted.

### Replacement output publication

Manual OCR, translation and production-TTS starts always create a fresh job,
even when an identical earlier job succeeded. The stable `output_path` remains
the active project filename, while each new job receives a job-scoped
`staging_output_path` (and `staging_qa_path` where applicable). Workers write
and hash only those candidates. A cancelled or technically failed replacement
therefore cannot mutate the active SRT/WAV.

On worker success, Job Manager validates the candidate against the immutable
job snapshot and QA hash before promotion. Existing active output and QA files
are moved to short-lived backups, candidates are atomically replaced into the
stable paths, and the project revision is saved. Any exception restores the
backups and leaves candidates available for diagnosis/resume. Legacy jobs that
lack staging fields keep the direct-output import contract.

PASS QA records also carry the owning job ID plus input/settings fingerprints.
Completed-job reuse requires that provenance and SHA-256 to match the current
stable file. Consequently, an older successful job cannot be mistaken for the
active result after a newer manual rerun has overwritten the same filename.

The project projection enforces dependency invalidation: replacing source SRT
removes translated cues and the active D1 projection; replacing translated SRT
removes the active D1 projection. This removes only project references. Voice
files, cue caches, checkpoints, job rows and immutable project revisions remain
owned by their existing stores. `MediaImportService` continues to replace S1
and D1 by stable track identity, so reruns cannot accumulate duplicate tracks.
Multi's ordinary coordinator path still reuses a matching successful stage;
explicit recomputation is a manual-stage intent rather than a batch default.

A manual production-TTS rerun has an explicit preparation boundary before job
creation. The client disables voice playback in Voice Studio, Timeline and
Multi; Job Manager then removes D1 plus its voice-media projection and commits
the next project revision without deleting the backing WAV or cue cache. Only
after that snapshot is applied does the client create the replacement job. This
prevents Qt Multimedia from retaining a Windows file handle that would reject
the final atomic promotion. A preparation failure creates no TTS job.

### Multi video orchestration

SQLite schema version 5 adds durable `multi_project_exclusions` alongside the
schema 4 `multi_batches`, `multi_batch_items` and `multi_stage_attempts`
records without changing the existing project, job or checkpoint owners. The
batch owns the secret-free configuration snapshot,
state, current project and deferred-pass marker; ordered membership owns
selection, stage, aggregate progress, child job, retry and error state; attempt
rows preserve the stage/pass/job history. The executable schema and migration
remain authoritative in `src/ducai_studio/infrastructure/database.py`, while
`src/ducai_studio/infrastructure/multi_video_repository.py` is the only
repository for this state under the Job Manager's single-writer rule.

`MultiVideoCoordinator` in
`src/ducai_studio/job_manager/multi_video_coordinator.py` is an orchestration
layer over normal OCR, translation and TTS jobs, not a second provider
pipeline. It admits exactly one child at a time in persistent membership order,
advances a project through all three stages, and delegates fingerprint/cache
decisions to the existing job factories. Immediate retry and the single
deferred final pass are coordinator policy; child checkpoints and immutable
artifacts retain their existing contracts. Pause first creates a durable
`pause_requested` boundary and gives the active child a bounded cooperative
checkpoint window. If a synchronous provider call prevents acknowledgement,
the coordinator transitions that exact child to `interrupted`, stops its owned
process tree, and publishes `paused` only after the PID exits; resume restarts
the same job from its last durable checkpoint/cache. Job Manager startup converts an unresolved running batch to
`interrupted`, and only an explicit resume reloads the stored batch snapshot.
An unexpected live child transition to `interrupted` follows the same boundary:
the coordinator closes the current attempt once, moves the batch to
`interrupted`, and waits for an explicit resume. It never immediately relaunches
an interrupted job. Resume requeues that exact job once and reuses its latest
fingerprinted checkpoint. Attempt insertion also reconciles duplicate `running`
rows left by older versions so historical rows cannot appear as fake retries.
When an OCR or TTS job stores an engine-host path inside an older packaged
`DUCAI-STUDIO-*` release, Job Manager supplies the current release host and its
SHA-256 to the worker without rewriting the durable job specification or resume
fingerprint. This keeps compatible checkpoints authoritative across upgrades;
an explicit user-owned custom host path is never replaced.
`ocr_review_required` is classified as a non-blocking deterministic QA result.
The server validates and imports its immutable review SRT before forwarding the
terminal event, then the coordinator records the Multi attempt as accepted and
advances directly to translation without consuming retry budget. Items deferred
by an older release are adopted the same way on resume instead of re-running
OCR. Missing/corrupt review artifacts fail during import and remain technical
errors; transient worker, runtime and resource failures keep the normal retry
budget and deferred final pass.

The server persists each worker event through the normal job event path before
forwarding it to the coordinator. Progress is mapped into thirds for the three
stages using the worker's `fraction` or `current/total` contract and remains the
durable aggregate used by orchestration/resume. The card view derives a separate
0–100% value for its current stage, so OCR, translation and TTS each start at 0%
and finish at 100%. Noisy card updates are coalesced by a 100 ms status timer
before a lightweight versioned
`multi_status` IPC event is broadcast. Subtitle cues and preview media are
loaded only through the per-project `multi_preview_descriptor` request, so a
large card list cannot exceed the IPC frame limit. Terminal child events
schedule the next coordinator decision rather than allowing QML to infer
sequencing.

Start, pause and resume commands use an optimistic ViewModel projection before
the IPC round trip, while their server responses contain only the lightweight
batch/item status rather than the full project catalog. Pause/resume projections
preserve the current child stage and checkpoint identifier; only a genuinely new
batch resets selected cards to OCR. Manual edits of a Multi translation review
also update the visible CJK count locally, then request a lightweight authoritative
status projection keyed by project revision. The durable failed review row remains
unchanged until the coordinator accepts the repaired translation and advances it
directly to TTS.
The lightweight status projection also carries the authoritative child job
state and cache-restore phase for the current card. The UI can therefore
distinguish **Đang khởi động bộ máy**, **Đang khôi phục cache**, **Đang tạm
dừng**, **Đã tạm dừng** and **Đã gián đoạn** without inferring state from retry
row counts. A paused label is shown only after the durable batch and child state
confirm that boundary.

Multi translation jobs carry an explicit `qa_policy=multi_auto` snapshot. The
worker keeps CJK detected in the final Vietnamese text as the only semantic
blocker, retries that targeted checkpoint, and gives overlong translated cues a
single compaction pass after CJK is clear. Name, Latin, style, Film Bible and
remaining length findings retain `original_severity` as evidence but become
warnings; a warning-only translation publishes and advances directly to TTS.
Technical coverage/format/cache failures remain retryable blockers. The
coordinator retries CJK review only when the failure payload includes
`cjk_cue_ids`; after the immediate budget and one deferred final-pass attempt,
an unresolved video is marked failed while the batch advances independently.
Gemini Web Multi snapshots additionally force `headed=false`; the manual
translation toggle remains independent and defaults to background mode.
The coordinator reapplies these invariants when it resumes an older batch. An
active translation job whose provider config still carries legacy QA is
cancelled at its safe checkpoint, receives the bounded `multi_auto` retry
policy, and restarts the same job/ledger; that upgrade does not consume the
provider retry budget.

When a Multi translation ends in CJK review, the server imports a complete
`translation_review` SRT into the project before publishing the terminal child
event. `JobManagerService.get_manual_translation_review_state` validates that
the project still contains every source cue with identical IDs/ranges and
returns the current CJK cue IDs from the edited Vietnamese text. Translation
preview/QA overlays that state on the immutable ledger, so the editor never
shows a stale CJK row after a manual fix. The Multi coordinator accepts a ready
review as a `manual_override` attempt and advances directly to TTS; it does not
retry or retranslate the job. Partial, malformed or non-review subtitles do not
qualify and remain subject to the normal CJK retry policy.

Large `get_project` responses use a transport envelope rather than raising the
fixed 16 MiB IPC frame limit. The Job Manager serializes JSON, records project
ID/revision, uncompressed and compressed sizes and SHA-256, then returns
`zlib+base64+json` when the framed envelope fits. Deliberately uncompressible
snapshots are written to a unique staging file under the owned artifact root
and returned as a `file+zlib+json` descriptor. The GUI validates path, size and
checksum in a single-worker `QThreadPool`; only the matching generation/project
can publish the decoded model. Timeout, disconnect, decode failure and stale
responses all release the busy gate without changing the current project.

Opening Multi synchronizes every project in the database that owns a video into
the durable membership list. A project without video is omitted. Auto-discovered
cards are unselected; explicit import from the Multi tab accepts one or more
video paths and selects the resulting/reused projects. Removing a card records a
project exclusion rather than deleting any project, media, job, checkpoint or
cache; the UI exposes restoration of hidden eligible projects. Lightweight card
status is enriched from one latest active-or-terminal job query per refresh and
artifact presence, so manual work is visible without decoding large provider
configs or resetting the GridView.

Multi configuration may persist provider/model/voice settings and credential
reference names, never credential values. Batch start applies the normal
secret-field rejection, validates referenced names through the credential
store, and workers resolve values at execution time. The service and server
guard manual OCR/translation/TTS starts at the stage boundary, rejecting only a
duplicate active stage for the project currently owned by Multi. The view model
requires explicit acknowledgement before a task for another project (or a
different stage) competes for CPU, GPU, VRAM or network resources.

The Multi UI uses one shared `MediaPlayer` and switches its source/output when
the active card changes; it never creates one decoder per card. Card images are
requested only near the viewport through
`src/ducai_studio/ui/multi_thumbnail_provider.py`, rendered asynchronously and
reused through the preview tile cache keyed by the media fingerprint. Video
playback requests the selected project's cue snapshot on demand, while D1
preview is audio-only.
Detailed edits deliberately open one project in Timeline, and card removal
changes membership only. Export is outside the coordinator contract.

## Provider rule

Providers own transport or inference, not product policy. Shared application
pipelines own OCR consensus, translation cue validation, voice timing/QA and
export snapshot integrity. This keeps Paddle/Rapid, Gemini Web/CLI/API,
OpenAI-compatible DeepSeek/Hy3/Qwen/GLM/custom transports, Piper/VieNeu and
preview backends replaceable without rebuilding screens.

Provider configuration persisted with a job may contain credential IDs or
references, never API keys, tokens, passwords or private secrets. Provider
workers resolve credentials in their own process at execution time. Persistence
rejects secret-shaped field names and embedded assignments, while worker error
codes are limited to bounded identifiers; raw diagnostic text never belongs in
the error-code column.

## OCR runtime boundary

The GUI and Job Manager use Python 3.12. PaddleOCR and RapidOCR run in a
persistent Python 3.11 engine host over a versioned line-delimited JSON
protocol. The OCR worker sends bounded timestamp batches and normalized crop
regions; no per-frame PNG files are created. A project may replace the default
dialogue band with one normalized frame-backed region and may define multiple
exclusion regions. The engine host maps exclusions into each crop and blacks
them out on a copy immediately before inference, so it neither mutates decoded
frames nor lets logo/watermark evidence enter dialogue or scene consensus.
Scene scanning also excludes the configured dialogue region, making dialogue
S1 and scene-text S2 disjoint by construction. The worker owns control
acknowledgements and technical evidence snapshots. Application code owns
two-frame consensus, multiband conflict rules, targeted Quality rescue,
overlay learning and the final SRT gate.

Dialogue reading mode anchors on horizontal glyph rows using pixel aspect ratios
(not normalized x/y lengths on a shallow crop), retaining adjacent wrapped lines
and short same-row fragments while excluding tall map/scene columns. Finalization
can collapse a weak mixed-row candidate only with repeated high-confidence clean
text and two matching source/frame/timestamp observations whose taller mixed boxes
contain the dialogue. The clean occurrence range is retained; audited evidence IDs
remain available, but the restored region uses clean boxes, not the noisy union.
One-frame duplicate-glyph insertions require three clean frames and a matching
source-frame region before restoring an observed alternative. Nested contradictory
overlaps all receive review state. The aggregation fingerprint separates new jobs
from older finalized checkpoints without deleting evidence or cache.

Sparse Quality range reinforcement excludes scene evidence explicitly: a scene
title may repeat a name from dialogue, but cannot extend that spoken cue into
later lines. This boundary also applies when finalization reconstructs candidates
from a checkpoint containing the later scene-Quality pass.
Fast scan evidence and Quality evidence are never blindly merged. A conflicting
reading remains a review blocker. REVIEW output is stored as an immutable
technical SRT plus QA artifact and loaded into the cue editor without receiving
the `_ZH_FINAL.srt` name. Coverage uses distinct subtitle-positive sampled
timestamps; upper/lower evidence at one timestamp counts once, and a timestamp
is covered only by an overlapping cue with agreeing text. Only a non-empty PASS
at or above 95% is atomically published with `_ZH_FINAL_QA.json`; the Job Manager
verifies job fingerprints, counts, path and SRT SHA-256 before auto-import.

The Multi coordinator snapshots the same OCR sampling rate selected by the
single-video screen; it never silently substitutes a second FPS policy. Video
sampling keeps the existing sequential OpenCV decode path for adjacent
timestamps and reserves keyframe seeks for genuinely sparse jumps. The bundled
Paddle host submits crops from both subtitle bands together when its runtime
exposes the batched API, while retaining the per-band fallback for compatible
external engines. These optimizations change only inference batching, not
timestamps, evidence IDs, consensus, or the OCR QA contract.

Image batching is not presumed recognition-neutral: even identical-size inputs
can produce different detector geometry and recognized text on GPU. Keep serial
detector execution inside each predictor. The accepted execution optimization
distributes crops to three persistent processes, not independent video chunks:
decode, exact-byte cache and cue/QA decisions retain one owner so there is no
cross-chunk boundary consensus loss. A request timeout or broken predictor pipe
may rebuild the pool once per runtime: stop every owned predictor, recheck free
RAM/VRAM, then replay the entire request with the same input order. Partial
results never enter the reading cache or evidence. Startup/resource/semantic
failures and repeated faults replay through the unchanged serial recognizer
rather than reducing samples or precision. Resource-triggered single-predictor
fallback keeps CUDA in one owned child, not an in-process model whose allocator
may retain VRAM after reference release. At recognition boundaries it probes no
more than once per five seconds and, with at least 4096 MiB free in **both** RAM
and the selected GPU, adds exactly two predictors. Handshake success publishes
the new count atomically; partial failure reaps only the new children and retains
the healthy predictor. Resource/transient expansion failures back off from 60 to
300 seconds; semantic/driver failures do not repeatedly expand. Promotion keeps
the decoder, change tracking, reading cache and one-time transport-recovery
budget intact. Explicit single-worker/CPU selection is never promoted.
No serial CUDA model is loaded beside a replacement pool. The standalone host's
private framed pipes carry trusted local
process data only. OCR client ownership reuses the pre-owned launcher and
Windows kill-on-close tree guard, including every external interpreter and
predictor descendant. Execution and resource selection are owned by
`ParallelPaddleRuntime` and `_paddle_runtime` in
[`ocr_engine_host.py`](../src/ducai_studio/engine_hosts/ocr_engine_host.py).
The frozen-input real-worker comparison is
[`ocr-speed-benchmark.py`](../tests/fixtures/ocr-speed-benchmark.py); release
evidence belongs in the active plan, not in a universal throughput guarantee.

`VideoSession` additionally owns a byte-bounded decoded-frame LRU, indexed by
the same rounded integer source-frame index used by the decoder. Its budget is
at most 256 MiB, no more than one thirty-second of available RAM above a 2 GiB
reserve; unavailable/low-memory probes disable it. First reads store a private
copy, and hits return another private copy, so crop/mask callers cannot mutate
reused pixels. Hits do not advance the physical decoder cursor. Eviction, mode/
media replacement and close release the cache; cache allocation failure disables
only the optimization. Frame targeting, final-frame tolerance and uncached
decode/seek errors retain their existing behavior. This is independent of the
exact-byte model-reading cache and never equates similar frames or skips samples.

Host `open` adds optional `execution_device` and `frame_cache_limit_bytes`.
Recognition responses repeat the effective device and worker count after
inference, including optional allowlisted fallback/recovery reason codes and a
bounded recovery-attempt count, so a mid-request fallback cannot leave stale
activity telemetry or hide its cause. Raw framework messages are not published.
Older responses retain the verified open metadata.
Worker scanning progress repeats the execution device/worker count and a readable
phase detail; compatibility CPU fallback remains visible after subsequent Fast
updates. Older host metadata does not invent a device. Runtime diagnostics runs
a hidden, isolated, bounded CUDA/cuDNN arithmetic probe without loading OCR
models or changing runtime installation. Its fixed diagnostic-only launch route
uses the pre-owned byte gate and Windows Job Object, so timeout/close stops the venv's real
interpreter, not merely its launcher. Package presence alone cannot produce a
GPU-ready report. These optional fields preserve protocol version 1 and do not
change SQLite schemas, evidence or QA decisions. The basic probe cannot certify
every model kernel or the accuracy/performance of untested GPU hardware.
The diagnostic launch route remains available before activation, accepts no
arbitrary Python code/job arguments, and does not unlock protected worker routes.

The host's optional `open.frame_count` identifies finite decoded media. The OCR
worker validates its duration against frame count/FPS, resets the bound on each
media/model switch, and filters requests only after mapping Timeline timestamps
to source time. Primary OCR, per-band schedules and change probes share this
bound. Valid timestamps and crop settings remain unchanged; no-frame tail
batches advance logical progress without fabricating evidence. The reader's
existing final-frame tolerance and corruption/seek errors remain strict. Hosts
without a positive finite frame count retain their legacy behavior. Logical
Timeline duration is not truncated; QA records `decoder_video_duration_us` and
`decoder_tail_request_counts` as diagnostics, not QA acceptance decisions.

Short-cue recovery keeps one sequential decode stream but samples inexpensive
96x24 grayscale signatures every 200 ms between regular Fast OCR timestamps.
Only the normal Fast timestamps and bounded transition offsets reach Paddle;
the intermediate frames never become OCR inference calls. The worker caps
accepted dialogue transitions at 48 per 30-second window and OCRs six bounded offsets
from 100 ms before through 400 ms after each transition. It requires two
high-confidence stable burst boxes
before a one-character cue can enter consensus. There is no independent second
full-video decode pass: burst and scene samples are decoded together inside the
current Fast window. Transition history is part of the checkpoint and is rewound
with the matching evidence, so pause/process restart cannot lose earlier adaptive
samples. A separate cross-pass bridge handles multi-character
dialogue that appears on one Fast timestamp and one Quality timestamp: both
readings must be high-confidence, temporally close, text-compatible and spatially
aligned. It never applies to one-glyph evidence and it flows through the same
targeted rescue merge as ordinary Quality candidates.

The scene-OCR setting defaults ON and is frozen for manual and Multi jobs. OFF
skips scene discovery, change signatures, regional rescue and generated scene
output, not S1's adaptive/short-cue sampling; toggling never deletes existing cues.
The host caches at most 256 lightweight model readings keyed by exact crop bytes,
shape, dtype and reading mode. Repeated inputs in/across batches skip inference;
timestamps, evidence IDs, geometry and scene metadata are rebuilt per observation.
Empty readings are cached too. Media/model changes clear the cache. No similar-
image shortcut or reduced sampling density is used to improve speed.

Scene-text discovery samples the full frame at up to 24 transitions per 30 seconds
with 0/240/600 ms offsets and three-second static anchors. These samples share the
chronological Fast decoder; S1's 48-transition/six-offset policy is unchanged.
The scene engine retains plausible horizontal text on textured backgrounds rather
than requiring a clean panel before recording evidence. Explicit dialogue masks
replace the old bottom-band heuristic and apply to all regional crops, together
with global user exclusions. Stable transient labels become `role=scene_text` cues with normalized
glyph/panel boxes, orientation, typography hint, sampled panel colour and review
state. Low-confidence spelling variants may join the same short spatial cluster;
one strong reading or two moderate Quality frames are required to retain a cue.
A single non-corner card may
be accepted only with exceptionally strong OCR and visual evidence. Horizontal
candidates with weaker visual evidence remain review-only; ambiguous middle-frame
dialogue without panel/placement evidence is not promoted automatically.

After dialogue Quality, scene Quality reads only expanded discovered regions at
2x scale, batching at most the configured window's crop count in chronological
order. It samples 400 ms locally, with 800 ms surrounding context. Vertical glyph
columns and adjacent horizontal fragments retain separate spatial identities.
Repeated one-glyph labels can be retained; two Quality frames at moderate OCR
confidence preserve uncertain spelling for review, never silently auto-accept it.
Crop results must cover at least half of the discovery target; neighboring
columns and crop fragments cannot become independent rescue candidates. Clear
full names confirmed on two frames outrank their cropped prefixes. Spelling
support is normalized per provider so a denser Quality schedule cannot simply
outvote clearer Fast evidence. Conflicting strong readings remain review-only.
Persistent platform anchors suppress overlapping scene fragments within the same
media/segment scope, not arbitrary names elsewhere in the frame.
No independent dense full-movie Quality scan is added.

Positive evidence and optional `record_kind=scene_probe` observations share the
checkpoint NDJSON ledger; empty probes are not dialogue evidence. Matching source
scope, geometry and text define occurrences, with a maximum 3.5-second gap and
two observed missing frames separating repeated labels. Nearby absent probes
bound the label's display range, replacing the fixed tail when evidence exists.
Rescue ranges clamp to retained scan segments and cannot bridge cuts/media.
`scene_quality` checkpoints rewind only regional scene rows/probes, preserving
completed S1 Quality evidence. The aggregation version in settings fingerprints
prevents old scene checkpoints from being reused unchanged. Old artifacts remain.
Dialogue hard-sub coverage still excludes scenes and is not scene recall.

Dialogue is projected onto S1. Scene
text uses deterministic interval partitioning across S2…Sn: simultaneous cues
cannot share a lane, while later non-overlapping cues reuse the lowest lane.
Automatic allocation never consumes S1. An explicit user move of scene text to
S1 promotes the paired source/translation cues to `role=dialogue`; moving a
dialogue cue away from S1 never demotes it. Role remains the semantic authority,
so TTS/SRT do not infer speech from the display lane during load or export.
Immutable cue/clip IDs and media references remain stable. Legacy dialogue cues
missing role fields retain their compatibility default. Legacy scene cues with a
missing lane, or simultaneous automatic cues overlapping on one lane, are
normalized on decode by reserving valid/manual lanes and interval-partitioning the
remainder across S2…Sn. The compatibility projection rebuilds only subtitle tracks;
cue IDs, timing, geometry and the stored revision remain stable, so no SQLite
migration is required. Translation retains
the scene metadata and Film Bible identity. User-facing SRT export, TTS and D1
filter to dialogue cues; guarded worker SRT artifacts remain an internal transport
that can carry aligned S2 rows while publishing the full project snapshot.
All subtitle cues may carry `text_font_size` (positive integer, default `12`,
without a product cap of 100),
`text_orientation`, `background_color`, `text_color`, `style_hint`, optional
`background_padding_x/background_padding_y`, `background_style`,
`letter_spacing_percent`, `font_bold`, `font_italic`, per-cue text/background
opacity and an optional stable display-track ID. Legacy snapshots decode these
new fields to the former solid, non-spaced, non-styled appearance. Legacy S2
snapshots retain their saved size; newly recognized scene cues use size `40`.
The OCR QA `cue_regions` optionally carries `text_font_size` because SRT cannot
encode typography. PASS and REVIEW imports accept only positive integers on
timestamp-matching rows. Older QA without this field uses `40` when classifying
a freshly imported dialogue-default cue as scene text, not during project load;
existing scene sizes and dialogue defaults remain unchanged. Legacy S2
`scene_font_scale` remains readable and participates in automatic
source-glyph sizing, while the editor size is a monotonic relative multiplier
resolved against video geometry rather than a desktop-pixel promise. The panel
is a placement/hit-test box, not a font-size clamp; only the video frame and a
large renderer-safety bound limit paint. Scene backgrounds use measured
glyph bounds plus per-cue padding instead of filling the panel. Preview and export
map `calligraphy`, `calligraphy_soft`, `calligraphy_bold`, `calligraphy_ink`,
`calligraphy_brush`, `calligraphy_water`, `calligraphy_splash` and
`calligraphy_hurricane` to the bundled Playwrite VN, Dancing Script, Lobster,
Agbalumo, Comforter Brush, Water Brush, Splash and Hurricane fonts. The
`ink_splash`, `ink_wash`, `ink_cloud` and `brush_stroke` backgrounds use a
one Qt-measured glyph/background path in `providers/subtitle_artwork.py`.
The UI image provider rasterizes that path; FFmpeg receives the same path as
ASS vectors, with explicit colours/alpha and no second libass font layout.
Windows worker artwork uses the Windows Qt platform unless explicitly overridden,
so system families such as Arial resolve exactly as in the native GUI; offscreen
Qt cannot enumerate the same installed font set. Non-Windows workers retain the
offscreen default. The render contract invalidates old font-fallback chunks without
deleting their cache. Real project/settings/QML-to-MP4 regression checks compare
glyph bounds with a one-pixel codec tolerance rather than byte-identical pixels.
Final cue typography/geometry overrides source style. Cached artwork is bounded
and refreshed on paused visual edits. Dialogue fits its final panel/video area,
while short sentences below 70 characters are not automatically wrapped.
The shared layout also returns measured line-placement and painted selection bounds.
First dialogue movement seeds each selected cue from its own rendered placement,
never its source OCR box; explicit panels remain authoritative. Alignment and
baselines survive the transition to a custom panel. Pointer gestures freeze their
initial dimensions and include the final release position. Large selections use
the same font/line metrics without constructing outline paths, measured off the
GUI thread with a frozen style and generation/project guard. Quit/job flushing
waits for measurement and the command ACK; stale ACKs cannot revive a gesture.
Format-size growth
expands a dialogue panel around its anchor, shared by the optimistic model and
atomic command, so a drag-created panel does not cap later font edits. Painted
selection bounds observe selectedCueIds rather than an imperative-only selection
query. Playback transport reserves separate bottom chrome outside video geometry.
Dialogue solid backgrounds use the union of line metrics and ink bounds plus
comfortable per-cue padding, not a glyph-shaped silhouette. Translated concealment
contains that padded line box. Preview strokes behind the fill in a tight temporary
layer and composites text alpha once; zero outline never creates a cosmetic pen.
This preserves thin foreground glyphs and independent text/background alpha without
expensive boolean operations on calligraphy outlines. Foregrounds below 3:1 contrast
against a coloured background with opacity >=50% render as contrasting white/black;
stored cue styles remain unchanged. Full-frame ASS masks keep odd dimensions, unlike
crop-based manual overlays which require even dimensions.
Export ledger/cache fingerprints include the measured-glyph render contract.
A matching legacy-input checkpoint is safely re-rendered instead of mixing old
chunks with new layout; cache files themselves are never deleted by this upgrade.
End-cap coordinates scale from the short-axis
thickness while only the centre span expands, preventing long/short labels from
stretching the brush ends. No per-cue raster input or decoder is created.
New `scene_text` cues created by OCR or the manual `T` tool resolve shared domain
defaults to `calligraphy_brush`, white text and a 100%-opaque black `ink_splash`
background, with a 300 ms entrance/exit fade enabled. These creation defaults do
not rewrite decoded legacy cues or explicit per-cue overrides.
Scene-text cues optionally persist `fade_enabled` and a per-end
`fade_duration_ms` (default 300 ms). Preview and ASS export clamp fade to half
the cue duration, apply it only to the translated text/background, and keep the
manual/dialogue concealment independent. `RenderSceneText.source_blur_enabled`
defaults to true only for backward-compatible decoding of old render jobs.
New snapshots disable it for every scene cue, including OCR-generated and legacy
project cues; Preview has no scene-glyph blur node. Chosen ink/solid backgrounds
remain, without promising reconstruction or complete concealment of source text.
One undoable subtitle command updates paired
source/translation cues and the matching Timeline clip, and returns a compact
patch instead of rebuilding large models. Multi-selection stores stable cue IDs;
pointer movement remains local to QML and one release commits one atomic format,
geometry or time/lane command. Shared move/left-edge/right-edge deltas are clamped
against the whole selection before preview and committed for every paired cue in
one project revision. Deletion removes the pair, clip and generated
background; double-click navigation resolves the same cue identity from Preview
or S1/S2/S3. Text editors pin the cue ID on focus entry and use non-restoring
focus bindings, so focusing an editor cannot restore an empty default and delegate
recycling or post-save sorting cannot redirect a pending edit to another row.
Selection APIs receive an explicit additive boolean; plain click is exclusive and
Ctrl+click is the only toggle path.

Timeline preserves and clamps the prior `ListView.contentY` across model resets;
cue selection never calls `positionViewAtIndex`. Timeline Text owns per-cue and
group typography/background/fade editing; SubtitleEditor retains content/add
editing but not the duplicate formatting toolbar. The typography toolbar remains
allocated even without a current cue. Overview body pan samples fixed parent
coordinates rather than coordinates relative to its moving thumb; toolbar
wrapping and split-pane minimum heights keep the overview inside the hit-test
area at 1180×720. Timeline clip selection is intentionally
seek-free; a plain press on empty track space clears cue/clip selection immediately,
and release seeks to that position. The clip presentation model intersects both
the horizontal time range and a throttled vertical track viewport with one-lane
prefetch, avoiding delegates for off-screen S-tracks. A bounded visible-row bank
reuses delegates; its row order is not chronological (the full track time index
owns queries). Materialization waits during a drag/resize or open context menu,
and clip menus are instantiated only on demand. Role values and cue-selection
membership are cached; bulk typography uses indexed cue lookup and one model
notification rather than a full-row scan/refresh per cue. Scene timing preview is
QML-local and snaps an approaching start/end edge to the playhead within eight
screen pixels, capped at 500 ms without a minimum time floor. Edges already in
that window at gesture start can move away freely. Pointer release samples the
final coordinates and sends exactly one command. Captured cue timing baselines
remain stable through model projection; only the gesture's generation-matched
ACK/failure settles its preview, not unrelated text/style saves. A stale background
ACK already included in a newer durable head is acknowledged without projection
or rollback. Deferred job/export/close actions also wait for a pending timing ACK.
Legacy dialogue cues without an explicit lane use S1 consistently in both UI and
backend movement; scene cues default to S2. Existing timing/collision guards remain.

Independent titles persist in optional `ProjectSnapshot.title_cues`, use
`role=title_text` and a `TrackKind.TEXT` T1 track above V1. Legacy decoders default
to no titles. The empty T1 UI row is only an add affordance, never a database
track or a target of track deletion/locking. Titles cannot enter source/translated
subtitle collections or share their IDs, so OCR/SRT/translation/TTS replacement
preserves them without adding them to voice or translation inputs. The title
command adapter reuses scene typography with isolated transient lanes, restoring
all titles to T1; simultaneous labels may overlap without scene collision guards.
Title operations use the same compact timeline patches, history, retained/conform
time mapper, gesture ACK guards and Ctrl+S/dirty-only five-minute save policy.
Immutable cue collections retained by an edit are not scanned or sent again as
subtitle index maps; title-only patches remain independent of subtitle count.
Only the clip paint model maps canonical video/audio/subtitle/title timings
through conform; source clip lookup remains canonical for edits and tile sampling.

[`application/timeline_previews.py`](../src/ducai_studio/application/timeline_previews.py)
constructs bounded visible source-time buckets without media I/O. QML debounces
viewport requests by 120 ms and the asynchronous
[`ui/timeline_media_provider.py`](../src/ducai_studio/ui/timeline_media_provider.py)
decodes real FFmpeg thumbnails/waveforms into the existing atomic disk tile cache.
File stat/fingerprints and subprocesses run in the image-loader callback, not the
GUI descriptor path. Waveforms rectify and downsample to 2 kHz before rendering
an amplitude envelope on a square-root scale. Stable buckets allow pan reuse;
many conform spans share a source-bucket image with multiple Canvas paint regions,
not a decoder per cue. Image dimensions and negative cache entries are bounded;
missing/silent media never receives a fabricated graphic. Actual source intervals
respect trim/speed/cuts; conformed paint extents cover the full output duration.

Warm disk tiles bypass cold-render admission slots; uncached FFmpeg work retains
the same concurrency bound and atomic publication. Multi thumbnail timeouts and
OS failures produce an empty placeholder rather than escaping the Qt callback.
Partial end buckets sample their available midpoint instead of `end_us - 1`.
When a successful thumbnail decode produces no frame after the final frame PTS,
one bounded previous-frame retry requests a one-second window, scaling before
reversing frames and retaining the original overall timeout (codec GOP preroll
can add decoder work before that window). This only affects
Timeline images, not playback/export time mapping or media quality.
Hidden Preview workspaces skip cue/region/scene/title time queries and reevaluate
current state when shown. Active title/scene queries use the existing time index,
including optimistic moved-in/out cues, preserving row order and half-open ranges.

The shared Preview owns Space/click playback toggling and disables those gestures
during text entry, OCR region selection, color sampling or overlay manipulation.
Preview single-click/Ctrl selection uses the seek-free Timeline selection API;
double-click editor navigation remains seek-owning. Uncommitted geometry is canceled
when its grabbed cue disappears or Preview is hidden; cancellation never commits.
Released geometry remains pending until the dedicated `subtitleGeometrySettled`
signal follows its current generation's ACK/failure/disconnect. Unrelated text/style
ACKs cannot clear the local preview, and stale geometry callbacks cannot emit a
completion for the newer gesture.

The shared manual-cue dialog creates either spoken `dialogue` or visual-only
`scene_text` at the playhead. Manual dialogue inherits geometry and typography
from the nearest spoken cue and prefers S1; a timing collision allocates the
lowest free S lane without changing its spoken role. Manual scene text uses
normalized frame geometry and allocates a new lane below every known subtitle
track/cue. A bounded project-scoped FIFO accepts manual additions
while one subtitle revision is in flight and dispatches them after that atomic save
releases the command gate. The selected cue resolves back to its track index,
allowing Timeline to reveal S3+ after model resets without scrolling during a
pointer drag. A scene cue moved to S1 is promoted atomically in both source and
translated collections; lane occupancy is checked before commit. OCR
publication merges prior manual cues after newly detected cues and translation
seeds them as already translated, so rerunning either stage cannot overwrite or
send them to a provider. OCR dialogue/scene role separation, manual-cue origin
and region settings are optional snapshot fields; legacy projects retain their
previous defaults without a SQLite migration.

The Text inspector's explicit `set_cue_role` command is distinct from dragging
between subtitle lanes: it atomically changes selected source/translated pairs
to dialogue/S1 or scene_text/S2 while preserving all other cue fields and time.
Shared cue IDs take precedence over legacy index pairing. S1 dialogue conversion
also rehomes unselected colliding S1 scene cues to the lowest free S2+ lanes.
Post-conversion interval occupancy includes dialogue on every candidate lane and
previous rehomes; source/translation pairs and their clips change in the same
undoable command. All other cue fields, clip identity/source range and unrelated
clip revisions are preserved. Only touched automatic track names refresh;
custom names and track flags remain intact. A repeat is a no-op, including
preexisting mixed-role overlaps. Newly introduced destination-S2 collisions reject the complete command because
legacy decode normalization would otherwise relocate explicit scene lanes. Title
tracks remain independent. Pending text/style operations settle before dispatch;
an invalid blank draft remains visible and blocks conversion across focus-out,
without deleting the cue or saving into the next selection.
The command uses the existing deferred project draft, compact patch and unified
undo/redo history. TTS admission follows the new role; existing D1/audio/conform
and waveform cache remain untouched until the user regenerates voice.

### Whisper audio extraction boundary

Whisper is an explicit alternative evidence source under the durable `ocr` job
kind. Jobs persist `extraction_method=whisper_audio`, a separate provider/model/
runtime fingerprint and the immutable `large-v3` path, so neither OCR nor Multi
can reuse a frame-OCR checkpoint by mistake. Old jobs and Multi snapshots without
the field resolve to `frame_ocr`.

The GUI never imports `faster-whisper`. A pinned external Python runtime launches
`whisper_engine_host.py`, which emits one metadata/segment/complete JSON event at
a time. The worker first creates one atomic 16 kHz mono dialogue WAV, then writes
segments to an NDJSON ledger. Pause/cancel terminates the host, checkpoints the
ledger and never publishes partial S1. Resume rewinds five seconds to the nearest
completed segment boundary, streams again, then deterministically repairs overlap
and duplicates before publishing. CUDA float16 is attempted first; a verified
CUDA host failure rewinds the same ledger and retries once with CPU int8.

Whisper publication has its own QA receipt (`kind=whisper_asr`) covering job and
settings fingerprints, output hash and cue count. It does not claim hard-sub
frame coverage. The Job Manager verifies that receipt and the final cue timeline
before atomically replacing S1. The standard release includes only the small host
script; runtime/model installation is explicit, and portable packaging includes
them only with `-IncludeWhisper`.

## Translation runtime boundary

The translation worker owns exactly one transport instance at a time. Gemini
Web, Gemini CLI/OAuth, Gemini API and the shared OpenAI-compatible API transport
implement transport only; semantic grouping,
Film Bible state, prompts, exact numbered-ID validation, glossary updates,
translation QA and final publication remain in the application pipeline.

Established glossary identities are locked, including spelling and term kind:
later suggestions cannot silently turn a name/place blocker into a generic-term
warning. New terms remain additive. The prompt and merge owners are
`application/translation_pipeline.py` (`FilmBible.merge_terms`). Translation
stays sequential because independent requests do not share newly learned names
or role evidence; a consistency instruction cannot replace missing speaker
identity in the source.

New translation jobs snapshot the project's active style preset and up to 4,000
characters of custom rules into the settings fingerprint. Projects may also
persist up to 100 named style entries (`name`, `preset`, `custom_rules`) inside
`translation_preferences.saved_styles`; selecting one applies its values as the
active preferences. The optional `active_style_name` retains its identity even
when several entries have identical rules; only active preset/rules enter job
fingerprints. Style save/apply actions support optional compact preference-only
acknowledgements; older callers still receive the full project response. The UI
merges only the owned preferences, keeps revisions monotonic, and preserves live
cue edits and preference updates across out-of-order projection workers.
`auto` resolves from the Film Bible profile; old jobs without these
fields use `legacy`, preserving their ledger signature. Ancient presets add local QA for
inappropriate modern pronouns, reversed intimate titles and source-proven
`阿狗/阿猫/老狗` aliases. Only violating cue IDs receive one targeted style repair;
remaining findings are downgraded to warnings with original severity evidence.
CJK detection remains a separate blocker. Multi snapshots preferences per
project at batch start, so mid-run UI edits cannot alter an active batch.

Every completed chunk updates an immutable ledger checkpoint. A transient Web
failure closes the page/context and resends the exact unfinished chunk after a
bounded interruptible backoff. Repeated transport or format failures split only
at semantic boundaries until the failing cue is isolated. Authentication,
CAPTCHA/challenge, verified quota and malformed responses are distinct failure
types and cannot masquerade as one another.

Gemini Web transport policy is explicit in the job configuration. The free
Guest profile disables Codex fallback, opens a fresh chat for every request and
replaces the complete browser after five real submissions. Each translation reply
contains both translated rows and any unresolved QA blockers from the same model
pass. The deterministic local gate merges those flags and triggers targeted repair
only for failing cue IDs, so there is no second remote QA request per chunk.
The browser remains strictly sequential. A paid Codex fallback may still be
configured deliberately for another profile, but it is never implicit in the
free Guest profile.

Web translation defaults to background Guest mode with semantic chunks capped at
150 cues and 8,000 source characters. API/CLI defaults use 80 cues and 6,000
source characters to reduce request count while retaining bounded JSON recovery.
Signed-in mode keeps an isolated persistent
profile and Temporary Chat; Guest mode stores no account chat history. A visible
session may wait for manual login or CAPTCHA,
but the tool never enters credentials or solves a challenge. CLI prompts travel
over stdin in headless JSON mode. Gemini API requests use `x-goog-api-key`; the
OpenAI-compatible transport uses a Bearer header. The shared HTTP boundary adds
an application-specific `DUCAI-STUDIO/<version>` User-Agent and JSON Accept
header unless a caller explicitly overrides them; it does not request compressed
responses. Both resolve secrets from
Windows Credential Manager and never place key values in SQLite, settings, cache,
URLs or logs. An ordered, provider-scoped pool stores reference names only.
References rotate on verified quota or authentication failure; an unclassified
429 does not rotate. A verified gateway/CDN client block is reported separately
and does not burn through the credential pool. Known API presets use JSON mode and disable thinking where
the provider supports it, reducing malformed responses and unnecessary latency.

The translation UI keeps a visible credential reference separate from the masked
secret. Model discovery is an explicit background catalog request, not a paid
translation. The manager bounds concurrent requests, reads keys only in its
thread and returns sanitized model IDs/errors. No redirects forward credentials;
body size, pagination and network wait are bounded. GUI generations reject stale
provider responses and asynchronous ComboBox updates preserve manually typed
model names. Manual model entry remains available when discovery is unsupported.

After a parsed subchunk is checkpointed, the worker emits a bounded
`translation_delta`. The GUI requests only unseen delta events by monotonic
sequence and patches matching Vietnamese model roles in memory. This avoids a
full project revision per chunk; only the existing final PASS/REVIEW import
persists translated cues.

Changing resumable Web transport options does not discard a translation ledger.
The worker verifies the source/provider/model ledger signature, keeps the
contiguous QA-pass prefix, re-plans only the unfinished suffix and carries every
non-empty cached translation forward by cue ID. Only a PASS ledger with complete,
non-empty Vietnamese text and exact source
count/timestamps receives `_codex_vi.srt` and is auto-imported. REVIEW remains a
technical artifact, is visible in the bilingual editor, and cannot be renamed
into a false final by the worker.

Translation recovery is job-identity preserving. The cache index scans all
translation jobs for the current project, requires the exact current source
fingerprint and validates each immutable ledger independently. Therefore one
stale or corrupt checkpoint cannot hide other compatible ledgers. Queued, paused,
failed, interrupted and cancelled ledgers are resumable; the server permits the
cancelled-to-queued transition only for translation jobs. Selecting a cache binds
the GUI immediately to that job ID before the asynchronous preview arrives, and
the stateful primary translation control pauses or retries that same job rather
than creating a replacement ledger. A rejected retry releases the GUI busy gate
while keeping the cache selectable.

Malformed provider QA must not turn a recoverable cue into loss of a many-hour
run. Common Gemini severity variants are normalized at the provider boundary.
If a response contains every ordered translation row but its integrated QA
envelope is malformed, the worker preserves those translations, marks the whole
chunk `malformed_integrated_qa` for review and continues later chunks without an
automatic rewrite request.
If targeted repair still contains CJK, the worker keeps the original blocker,
checkpoints the QA-review state, emits a deferred-repair warning and continues
later chunks. The final completeness/CJK gate still fails closed; deferral never
promotes bad text into a PASS result.

The Multi policy is intentionally scoped to the coordinator snapshot and does
not change manual translation jobs. Multi adds `qa_policy`, `cjk_is_blocker`,
`auto_compact_overlong_once` and `max_overlong_compaction_passes` to the worker
configuration without a database migration. Legacy jobs omit these fields and
default to the legacy behavior, while new Multi jobs persist the policy in
their settings fingerprint so an incompatible result is never reused.
When a legacy checkpoint is adopted, semantic severities are normalized only
for chunks that already completed QA and whose complete cue set exists in the
translation map. Partially cached and not-yet-reviewed chunks remain pending,
preventing policy refresh from creating an impossible or unverified QA-pass
entry.
Attempts consumed by the known QA-pass/cache normalization defect are retained
as `superseded` audit rows, removed from the retry budget and resumed through
the same job ID and checkpoint. Recovery matches the exact deterministic error;
provider and content failures keep their original retry accounting.

## Voice runtime boundary

Piper and VieNeu never import their ML dependencies into the GUI environment.
Each runs in its own managed runtime behind a persistent engine host. Piper is
the fast default; VieNeu-TTS v3 Turbo 3.8.1 is the quality-first preset and clone
provider. Catalog auditions are static licensed WAV assets, so preview never
loads a model or requires a project. Verified Piper downloads run on Qt's worker
pool, land as `.part` files, and are published only after size, digest and config
validation; the QML thread only consumes progress events.
The catalog reports the real install/readiness state and preserves explicit
rights warnings instead of downloading community or real-person-labelled voices
silently.

Production synthesis is fingerprinted and cached per cue. Batching optimizes
transport only: QA, retry, onset repair, fit and blockers remain cue-scoped. A
resume reuses approved cache entries; an explicit “regenerate selected cue”
bypasses only that cue's global cache and then reassembles the complete voice
timeline. No eligible cue is silently omitted and REVIEW cannot auto-import a
partial WAV. Even when every cue is adopted from cache and no synthesis batch
runs, the worker commits the complete final timing ledger before `succeeded`.
Project import therefore retains one D1 clip per source cue, and the cue-scoped
source ranges remain available to preview and export without another provider
call.

WAV preparation may use three bounded CPU-only processes while retaining the
single GPU model and existing synthesis batches. `providers/tts/wav_preparation.py`
owns unchanged DSP/fit/QA; `postprocess_client.py` and `workers/tts_postprocess.py`
own hidden, pre-owned process lifetime. The parent TTS worker alone publishes
cache metadata and ledger entries, resolving normal competing owners under
fingerprint locks. Explicit selected-cue regeneration retains its existing
overwrite semantics. Private outputs are verified before publication; Windows
extended-path aliases do not bypass staging confinement. First-pass acceptance,
targeted retry and sparse signed checkpoints retain their serial workflow.

TTS admission uses a semantic `tts_input_fingerprint` over dialogue cue IDs,
timestamps, Vietnamese text, provider/model/voice and synthesis settings; the
project revision alone is not a cache invalidator. A visual-only revision can
therefore publish the same validated D1. Voice admission, payload construction,
rebase, D1 import and cache-only conform join source/translation cues by exact
identity in source order, never by list position. The join rejects missing or
duplicate identities and mismatched indexes; production also validates matching
time ranges and nonempty translations. Conform retains source timestamps as its
authority even when translated timestamps have drifted. Job creation persists
the latest working head, including manual text/timing/add/delete and cut edits,
before freezing its inputs. When dialogue changes during an active
production worker, the server records one replacement intent, stops the owned
worker at its next checkpoint, and creates one current-head replacement after
the old process exits. Per-cue cache fingerprints adopt every unchanged WAV.
Progress separates cache restore, model loading, warm-up and synthesis. ETA is
withheld until two completed warm batches and then uses the rolling median of
recent batch throughput.

Legacy global voice metadata is accelerated by a persistent SQLite sidecar.
This owner-written disposable cache index is an explicit exception to the live
project database's Job Manager-only writer rule; CPU preparation hosts open
neither database.
Modern fingerprint paths are checked directly; arbitrary legacy JSON filenames
are imported by one compatibility scan and a durable completion marker prevents
the 180k-file scan from recurring on later jobs. Sidecar failure never changes
cache authority or makes synthesis fail.

SQLite schema version 6 stores new project revisions as `zlib-json-v1` with
zlib level 3 while retaining the canonical uncompressed JSON SHA-256. The
decoder continues to accept `json-v1`; compressed payloads are size-bounded and
verified before JSON decode. Subtitle text/style edits patch the resident model
optimistically, coalesce compatible draft commands for 150 ms and serialize
compact IPC commands per project. GUI editor RPCs opt into `deferred` persistence:
ProjectRepository holds the latest immutable working snapshot in manager memory,
with its original durable head as a save precondition. Ctrl+S and a separate
single-shot 300,000-ms dirty-only autosave timer call `save_project_edits`. A clean
ACK cancels the deadline; clean projects make no periodic save requests. Edits
do not restart an already armed deadline and in-flight saves stop the timer;
remaining dirty changes or failures rearm one deadline. Atomic saves validate the
durable head inside the write transaction; failure retains the draft. Historical
durable revisions remain readable; intermediate working revisions are not written.
Undo/redo works on shared resident history. Project switching, imports, jobs and
shutdown flush draft commands and persist before taking a snapshot. Staged jobs,
TTS replacements and database optimization also enforce this barrier manager-side.
Deletion drops its draft only after a successful transaction. Save responses are
generation-bound and cannot mark later edits saved or launch a dependent action
until those later edits are also persisted. A watchdog releases the save gate
without reconciling away the unsaved draft. Force-ending a process can lose edits
since the last successful save; there is no crash-recovery journal for drafts.

The released migration-6 checksum remains immutable, but v5 upgrades execute
only its four constant-time column additions. The historical JSON size backfill
is deliberately deferred because it rewrote every multi-megabyte revision and
could generate multi-gigabyte WAL files before the local socket was available.
`json-v1` decoding does not require those size columns; the explicit optimizer
fills them while converting each row. The GUI retries startup for up to 60
seconds while the owned child is starting and suppresses transient local-socket
errors, but still surfaces process exit or a real startup timeout.

Database optimization is an explicit maintenance operation owned by Job Manager
and runs in a cancellable worker thread only when no job or Multi batch is
active. It copies the database beside the active file, compresses legacy rows in
batches, verifies table row counts, revision hashes, foreign keys and
`integrity_check`, then performs an atomic swap. Cancellation is checked during
copy, compression and `VACUUM` and once more immediately before the swap; the
active database is untouched when cancellation wins. The prior database remains
as `.a121-backup` until the next startup health-check succeeds.

VieNeu enrollment derives a local 192-dimensional speaker embedding and
reference codes from the selected reference audio. The NPZ and metadata are
written atomically and fail closed unless the binding receipt proves the exact
3.8.1 profile contract. Reference text is stored only as a user note because
the current inference binding is audio/profile-based. Profiles created by 3.3
remain immutable on disk; the 3.8 host removes the legacy encoder pad frame in
memory before binding when present.

Approved cue audio passes DC/silence cleanup, onset-click repair, edge fades,
segment normalization, an absolute speed cap and pitch-preserving fit. The
complete WAV is assembled on the integer-microsecond timeline by streaming PCM16
gaps and approved clips; memory therefore scales with one I/O chunk rather than
the full movie duration. The exact PCM bytes written to the timeline are also
fed to FFmpeg's first EBU R128 measurement pass, so assembly and measurement run
concurrently. The unchanged measured values drive the second dynamic `loudnorm`
pass. Assembly remains overlap-guarded and mastering remains two-pass. The
full-voice timeline override serializes clips by cue order before assembly and
extends the output duration when needed, so it never bypasses the overlap guard.
Only 100% eligible-cue coverage with a PASS report
replaces the project voice track `D1`; the track keeps the single mastered WAV
asset but stores one clip per measured cue interval, so Timeline can show and
edit each spoken segment without playing a second copy of the master. Qt
Multimedia preview seeks, pauses and mutes D1 with the active video clip, while
Voice Studio can also play the final D1 WAV directly.

VieNeu batches inference through one persistent host/model session. Batch
transport uses up to 32 cues per GPU request and halves the batch after a proven
out-of-memory error, but the application still evaluates each returned artifact
independently. Version 3.8.1 owns the fused CUDA Graph implementation upstream;
DUCAI no longer monkey-patches VieNeu private APIs. The runtime is pinned and
validated for CUDA 12.8, while application-level per-cue QA and ordered output
remain unchanged.

VieNeu inference always runs at 1.00x. The immutable synthesis cache stores that
base WAV after audio QA. A second variant cache is keyed by the base WAV SHA-256,
total read speed, processor identity, sample rate and QA settings. Changing the
global read speed or long-cue policy therefore never invokes VieNeu while the
base WAV remains valid, and every variant is rendered directly from the base
instead of accumulating time-stretch artifacts. Piper retains its provider-side
`engine_speed` compatibility path.

New UI jobs use a global read speed of 1.30x, selectable from 1.10x to 3.00x;
legacy jobs without the field resume at 1.00x. Optional adaptive timing measures
the base WAV duration `R` against the source SRT slot `S`. Its control is a cap on
the final total speed, not a delta: the applied speed is
`min(max(global_speed, R/S), cue_cap)`. Cues already fitting after the global
speed are not accelerated further. FFmpeg composes `atempo` stages above 2.00x,
preserving pitch up to 3.00x. Final WAV overflow up to 50 ms is tolerated for
sample rounding; larger overflow remains `CUE_SYNC_LIMIT_EXCEEDED`/`CUE_TOO_LONG`
with raw/base/final duration and required/applied-speed evidence. The worker
does not overlap, truncate or omit speech. A failed assembly retains its cache
and checkpoint for retry. Blindly re-inferring identical audio is not used as a
speed optimization. Internal error identifiers stay in the job record for
diagnostics; the desktop UI translates them into Vietnamese guidance. A missing
or changed TTS engine host also carries a Vietnamese reason and recovery action,
instead of collapsing the failure to the exception class name.

Automatic text condensation runs only after global speed, long-cue speed and a
fresh duration measurement. It submits only still-failing cue IDs and regenerates
only WAVs whose translated text changed. Translation requests carry source
`start_us`, `end_us`, slot duration and a soft character budget; these guide
concise Vietnamese but never form a hard truncation rule.
Managed raw WAV names carry a cue fingerprint prefix, and reuse additionally
requires the prior job's full ledger signature to match provider, model, profile,
all cues and pipeline identity. A new adaptive job may then restore the raw
artifact and rerun post-processing/fit without inference; explicit cue
regeneration bypasses this path. The legacy strict timing signature is
recognized so an in-flight a36 job remains useful after upgrading.

## Post-dubbing voice conform boundary

Voice conform is a cache-only stage after a production D1 has passed. The
canonical source timestamps and base cue WAVs remain immutable. A versioned
`VoiceConformPlan` stores the selected stretch-video, speed-audio or hybrid
policy; cue decisions; merged piecewise video segments; cumulative output
offsets; unresolved cue IDs; and a stable fingerprint. Audio variants use a
second cache keyed by the base WAV hash, applied audio speed, processor identity,
sample rate and QA settings, so changing mode or the 1.00x-2.00x control never
invokes the TTS provider. Variant publication is serialized per fingerprint and
uses a unique temporary WAV plus atomic replacement. Each variant is padded or
trimmed to the exact absolute sample boundaries in the plan before QA, preventing
microsecond/sample rounding from creating a false overlap.

Source/output point queries binary-search the validated monotonic segment ends;
the serialized plan, fingerprint, interpolation and boundary rules stay unchanged.
Export setup indexes winning video segments by link group once, while retaining
the complete hidden-video group set to suppress occluded linked original audio.

Estimate requests use a compact read path. Job Manager keeps the validated WAV
identity map in memory and reuses it while the user changes mode or speed, so
an estimate does not re-hash the full WAV ledger. IPC returns only summary
metrics and unresolved cue targets; the complete plan stays inside Job Manager
and is materialized for the actual conform job. This keeps QML responsive on
projects with thousands of cues without weakening cache or QA gates.

Timeline exposes the speed-audio policy as a dedicated **Khớp giọng** workflow.
Translated cue IDs and indexes are joined to the source SRT, but the conform
service always reads `start_us/end_us` from `source_cues`. Therefore each D1
decision remains anchored to the untranslated source microsecond slot even if a
Vietnamese cue range was edited after the base voice had been published.
After publication, the D1 track contains one clip per cue (all referencing the
published master/conformed WAV) and a virtualized cue model draws only the D1
segments near the current viewport (aggregating when zoomed out), labels the
actual per-cue audio speed and seeks through the source/output time map. Preview
uses a sorted time index instead of scanning every D1 cue, and live translation
refreshes publish contiguous model ranges rather than one QML signal per cue.
This keeps a 13k-cue film responsive without shipping the multi-megabyte
provisional plan through IPC on every slider movement.

The worker's `succeeded` event is imported while the durable job row may still
be `RUNNING`. Cue serialization therefore derives only from the assembly/QA
policy (`base_no_loss`, shifted cues, deferred conform, non-blocking QA or the
explicit overflow option), never from the later job-state transition. Master-WAV
assembly, source ranges and D1 timeline ranges consume that same serialized row
set, preventing a transient 20–30 ms overlap from rejecting an otherwise valid
cache-complete result. Resume reuses the ledger and WAV cache and performs only
assembly, validation and atomic D1 publication.

D1 timing color is a derived presentation state, not another persisted source
of truth. Against the authoritative source-SRT slot, duration overflow above
50 ms is red, underflow above 50 ms is yellow, a duration-matched positional
offset above 50 ms is yellow, exact/within-tolerance alignment is green, and a
legacy clip without a slot is neutral. The legacy `timing_unresolved` field is
still decoded for compatibility but new models recompute the state. Auto-fit
targets only red overflow clips; content warnings remain an independent badge.

Resize interaction is optimistic and UI-local. QML maps the pointer into the
shared clip-layer coordinate system, clamps the complete-source playback speed
to 0.50x–3.00x, updates clip geometry/color on every pointer event and displays
the SRT snap guide without IPC. The attraction window is at least 80 ms, at
least eight screen pixels at the current zoom and at most 500 ms. Mouse release
sends one resize command and creates one project revision; rejection clears the
optimistic state and restores the persisted clip. The backend retains an 80 ms
exact snap guard so non-UI callers observe the same minimum contract.

Early cue-split project revisions wrote deterministic
`voice-<index>-<cue-id-tail>` clip IDs but left the added cue metadata fields
empty. The presentation model restores that link only when the source cue index
and ID tail produce one unique match; ambiguous legacy audio remains unlinked
and is surfaced as such instead of being guessed into the wrong subtitle cue.

Overlapping source cues form one block and their voice clips are serialized in
cue order. The block expands only as far as required by the rendered WAVs. Video
cannot run below 0.50x, and cues exceeding the selected audio cap plus video
limit receive `CUE_SYNC_LIMIT_EXCEEDED`; application and export remain available
with a warning, while technical artifact/plan failures still block publication.
A conform result is
published atomically only after every variant WAV, the assembled D1, overlap
guard, duration check and final QA pass. Reset creates a new project revision
that restores the base D1 instead of applying another inverse transform.

The plan's monotonic source/output time map is the only timing authority after
publication. Timeline preview maps output positions to source video positions,
changes video playback rate only at segment boundaries, plays the already
conformed D1 at 1x and enables pitch compensation for the original soundtrack.
Subtitle and overlay ranges are mapped into output time. Export snapshots embed
the same conform segments: FFmpeg uses `trim + setpts + concat` for video and
`atrim + atempo + concat` for original audio, then composes blur/logo and burns
the output-timed ASS subtitles. Because `atempo` is pitch-preserving, the
original soundtrack follows the slowed image without lowering pitch.
Background music is output-time media: it is not stretched or source-time
remapped by voice conform, while original soundtrack audio is.

## Effects and export boundary

Logo and blur are normalized overlays in the versioned project snapshot. Video
import seeds both across the complete timeline from user-saved presets; a logo
remains disabled until its image asset exists. Geometry, opacity, blur radius,
enabled state and active range are real persisted values, not preview-only UI
state. Original audio, voice and music remain timeline tracks, so mixer changes
affect preview and export through the same volume/mute contract.

Frame OCR QA schema 3 stores optional full-frame normalized regions per source
cue and optional repeat-proven platform-mark regions; schema 2 remains readable.
Crop-local boxes are mapped to the source frame before persistence, while SRT
remains the sole timing authority. New tracked render blurs use `translated_text`
and resolve their rectangle from the final Vietnamese glyph bounds plus padding.
Legacy export snapshots default to `source_subtitle` and keep their recorded
rectangles. OCR watermark regions remain independent. Preview and export map cue ranges through
the same voice-conform time map. FFmpeg groups regions by blur radius and
strength, creates one blurred full-frame source per group and drives time-varying alpha masks,
rather than creating a full-frame blur branch per cue. New overlays use a
stronger radius/padding and optional `cover` concealment mode. Cover adds a
90% dark veil after the shared strong blur while legacy snapshots missing the
field decode as blur-only. The cover veil geometry and opacity are identical in
Preview and export; GPU/FFmpeg blur kernels and encoded-edge rasterization need
not be pixel-identical.
Preview scales the pixel radius to its rendered
canvas, while export applies the radius at output resolution. Both use
half-open cue ranges, so adjacent cues do not conceal an extra boundary frame.
Blur and cover composition remain before ASS subtitles.

Chunk materialization writes each time-varying rectangle group as an ASS vector
mask (and cover/fill layer where required). Libass activates only the current
events, so graph size and per-frame work no longer grow with every cue in the
10-minute chunk. Cue-scoped D1 clips that reference an uncompressed PCM master,
run at 1x/volume 1 and do not overlap are streamed into one chunk-local WAV with
sample-accurate silence and source ranges. The main graph then mixes one D1
input instead of hundreds of `atrim + adelay` branches. Unsupported formats,
speed/volume edits and overlaps retain the prior graph path. Chunk-local WAVs
are bounded to 512 MiB and removed after FFmpeg exits, including failures.

Export snapshot schema 4 carries accepted scene replacements separately from
dialogue subtitles. Newly compiled scene replacements disable source-glyph blur,
including OCR-originated cues; Preview draws no scene glyph-cover layer. Only
the chosen text/background artwork is composed. Legacy render-job snapshots
retain their recorded blur flag for decoding compatibility. S1 concealment,
watermark and independent manual blur are unchanged. Vertical
labels are stacked and use a source-derived automatic size multiplied by the
explicit positive editor value without a product cap of 100; moving or shrinking the panel does not silently
reduce that chosen value. Calligraphic sources use one of the bundled OFL
Vietnamese-capable brush faces; ordinary display text uses Be Vietnam Pro. Preview
follows the same glyph/panel geometry and hides replacement output for
`review` cues, which remain visible to the editor as a yellow review guide.

The OCR engine retains segment geometry until platform-watermark filtering is
complete. A fuzzy Bilibili recognition is a spatial anchor; only nearby
same-row compact fragments carrying a complete channel signature (handle with
numeric suffix, resolution/repair marker or a non-dialogue channel name) are
removed with it. Common OCR deletions/substitutions inside `bilibili` are
accepted. The finalizer learns a pure-CJK channel marker from pre-grouped OCR
evidence only after it repeats across multiple timeline regions at a frame
edge. This avoids broad keyword deletion and preserves dialogue containing
ordinary numbers or `漫剧`, even when it sits beside the platform mark.
Filtered watermark detections are persisted as ignored evidence in the same
checkpoint NDJSON, never entering cue consensus or coverage. Two distinct
frames with spatially clustered boxes produce a generated `ocr_watermark` blur
overlay; a new OCR publication replaces the previous generated set atomically
without touching manual effects. Export compiles these overlays into the same
grouped strong-blur/cover path used by subtitle regions, so it does not add one
full-frame blur branch per watermark. During scene scanning, a Chinese companion
glyph whose box substantially overlaps a Bilibili anchor from the same frame is
discarded before scene clustering; this removes split fragments such as `独` or
`独播`. If Paddle misses the Latin anchor, only the small known `独播` platform
badge forms in a frame corner are rejected; centered dialogue with the same word
is retained.

Dialogue fragments are clustered by visual row before x-ordering, avoiding
one-pixel y jitter rotating a caption. Candidate finalization and reviewed OCR
imports collapse same-region, strongly overlapping full/partial readings, including
bounded recognition errors in shorter readings and legacy circular fragment order.
They retain the winning source text, union timing and evidence; manual and scene
cues are excluded. Equal-length different complete sentences are not fuzzily
deduplicated. Cross-band temporal conflicts require textual agreement or strong
time containment: sampling padding alone must not swallow a short distinct line
touching the following longer caption. These are source-evidence rules, never
Vietnamese semantic deduplication.

The shared default upper profile ends at normalized y .982 with scale 1.0 and
dialogue reading mode; the lower profile remains an independent comparison.
Custom dialogue rectangles remain authoritative. `reconcile_dialogue_evidence`
runs before Fast/Quality grouping and finalization. A clipped reading can be
suppressed only by a high-confidence full reading in the same provider/model,
source scope, frame and glyph region, crossing the clipped edge while fitting
inside its own crop, with another nearby timestamp confirming its text. Raw
NDJSON is retained. Cross-band merging consumes all proven matching fragments;
crop clipping may distort glyphs while preserving character count. An equal
CJK-count full reading is eligible only when the clipped read is below the
existing 0.90 complete-reading confidence floor; reliable equal-length votes
remain available to consensus, including identical text. Geometry does not prove
that the uncut crop has the better transcription for the entire occurrence.
Shorter full readings and one-glyph winners remain ineligible. The original
strictly-longer early-suppression path is unchanged. Equal-count proofs are
deferred: all those evidence rows still vote in grouping, rescue, representative
selection and original occurrence normalization. Only then does
`collapse_proven_ocr_candidates` remove a whole occurrence. Every loser evidence
ID must resolve to one unique retained terminal occurrence, with unambiguous
ownership on both sides and complete, unmixed, matching source scopes. Preserve
the winner's identity, text, confidence and evidence; union only timing ranges.
Run overlap/review QA after this collapse, not before. Targeted rescans apply the
same proof-only collapse before ranking candidates by target overlap. Crop-edge
and geometric corroboration decide proof eligibility, never translated semantic
similarity or timestamp overlap alone.
Every emitted proof points to a retained winner. If overlapping crops each
appear to clip the other, or a proposed winner is itself a proven clipped read,
the dependent uncertain readings remain available instead of forming a deletion
cycle. Worker calls require shared source-frame geometry, not timestamp containment
alone. Sampling overlap resolution does not fuzzy-collapse distinct complete
lines without shared visual-frame evidence. Unresolved conflicts retain cues
and explicit review state.

QA schema 3 adds optional per-cue confidence/evidence IDs and geometric
suppression proofs. `deferred_reading_ids` identifies delayed proof eligibility;
`retained_deferred_reading_ids` makes votes still owned by final cues explicit.
A proof is not itself a claim that its row or occurrence was deleted.
`proof_only_collapse: true` selects strict reviewed import: every evidence ID of
the clipped cue must point to the same uniquely owned terminal OCR dialogue cue
on the same lane, and no legacy containment heuristic runs afterward. Scene and
manual cues are excluded. Legacy schema 2/3 reports without this flag retain
their prior import behavior. New jobs fingerprint `OCR_AGGREGATION_VERSION`;
the deferred-processing version rejects old-policy checkpoints without deleting
their immutable artifacts. No database migration is required.

Effect edits use a compact revisioned patch containing changed overlays/media
only. The stable overlay list model updates one row; dragging/resizing stays
entirely local and emits one command on release. Other controls apply an
optimistic local patch and roll back to the last value on failure. Per-overlay
generations prevent an older acknowledgement from replacing newer geometry,
and effect acknowledgements never trigger project, cache or preview rehydration.

Starting an export compiles an immutable projected snapshot with ordered video
segments, audio tracks, subtitles and overlays. Legacy one-video snapshots remain
readable. Sequential segments are trimmed, retimed and joined through the same
retained-footage map as Preview. Visible overlapping V-tracks are resolved by
[`domain/video_layers.py`](../src/ducai_studio/domain/video_layers.py): the last
visible track wins, falls back to the preceding layer outside its range, and
gates linked original audio. D1/music remain Timeline-scoped. Preview descriptors,
OCR scan plans and export consume the same resolved, chronological segments;
uncovered gaps fail closed rather than being silently omitted. The executable owner is
[`application/export_pipeline.py`](../src/ducai_studio/application/export_pipeline.py).
Each checkpoint chunk is generated by one FFmpeg
filter graph containing trim/speed, blur, logo, ASS subtitles and audio mix.
Cue-scoped clips that reference the same mastered D1 WAV share one seeked FFmpeg
input per chunk; their individual source ranges remain in the filter script.
This avoids the Windows command-line limit without merging cue speed or volume.
Blur and logo are composed before the ASS filter, so a region used to cover an
original hard subtitle cannot obscure the generated Vietnamese subtitle.
The provider probes whether NVENC is actually usable and falls back to libx264
only when the user selected `auto`; an explicit NVENC request fails closed.

Render progress comes from FFmpeg `-progress`, while elapsed and ETA use the
actual processed timeline duration. Completed chunks are probed, checksummed
and cached; restart reuses only matching playable chunks. Pause suspends the
owned FFmpeg process, resume continues it, and cancel retains the safe
checkpoint. Final concatenation uses the same owned process control and exposes
separate concat, probe, SHA-256 and publish phases. The local-delivery MP4 skips
`faststart`: moving `moov` to the beginning does not change media quality or
local playback, but would require another relocation of the complete multi-GB
file solely for progressive HTTP playback. Final SHA reads in interruptible
blocks so Pause remains responsive without weakening checksum validation.
Final concatenation writes to a same-directory partial path. A
finalization marker closes the crash window around atomic rename: restart can
adopt only an output whose checksum and playable duration match the same job.
The Job Manager accepts success only after ffprobe and the final QA JSON agree
with the guarded MP4 path and SHA-256. Every FFmpeg/ffprobe capability, render
and QA subprocess uses Windows `STARTUPINFO`/`CREATE_NO_WINDOW`; its bounded
stderr and return code remain available for diagnostics, so hiding the console
does not hide export failures.

The preowned worker launcher also explicitly uses Windows `CREATE_NO_WINDOW`
for its child, rather than relying on its ancestor's console state. Its launch
gate, inherited stdin/stdout/stderr, environment, process-tree ownership and
child exit code remain unchanged. Real Python, CMD and PowerShell probes cover
no-console, GUI and hidden-console parent processes; this validates console
detachment without suppressing worker diagnostics or intentional GUI dialogs.

The shared subtitle artwork contract includes optional black/blur/off dialogue
backgrounds. Editor and export payloads use the same cue geometry/style and bundled
font outlines; render checkpoint identity includes the contract version and font
file hashes. Older chunks cannot be reused under changed artwork.

Export snapshot schema 5 optionally carries independent `title_texts`; old schemas
still decode without them. Titles use the measured scene artwork/ASS path but are
painted separately from subtitle enable flags in Preview and export. Title-only
projects can export with subtitle burn-in disabled or no S1/S2 cues. Their ranges
are projected through the same retained/conform map as the other effects, with
no source blur or automatic translation. Shared font/layout inputs are checked
against real native Preview and an independently launched export worker.

Ordinary large subtitle patches prepare their canonical/projected snapshot on a worker, while
Qt models are committed only on the GUI thread. Cue-index and clip metadata
changes use compact dictionaries; clip ownership lookup is linear, not a nested
scan. Foreground subtitle generations remain active through projection; background
saves and effects wait for their committed patch before sending dependent deltas.
Disconnect, timeout and failed projection reconcile durable head and invalidate
dependent callbacks, so a late ACK cannot unlock a newer edit.
