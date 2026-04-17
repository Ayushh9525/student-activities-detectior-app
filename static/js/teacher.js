/**
 * teacher.js
 * - Gets teacher's camera/mic
 * - For each student that joins: creates a WebRTC peer connection
 *   (sends teacher stream → student, receives student stream → card video)
 * - Manages student cards with live video + AI status overlay
 * - Alert log + summary counters
 */

const socket    = io();
const ICE_CONFIG = {
  iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
};

// ── State ─────────────────────────────────────────────────────────────────────
let localStream = null;              // teacher camera+mic
const peerConns = {};                // sid → RTCPeerConnection
const remoteStreams = {};            // sid → MediaStream
const students  = {};                // sid → { name, status, ear, alert_type }
let alertCount  = 0;
let isMuted     = false;
let isCamPaused = false;

// ── DOM ───────────────────────────────────────────────────────────────────────
const localVideo      = document.getElementById('teacher-local-video');
const studentGrid     = document.getElementById('student-grid');
const waitingMsg      = document.getElementById('waiting-msg');
const studentCountEl  = document.getElementById('student-count');
const alertList       = document.getElementById('teacher-alert-list');
const noAlertsEl      = document.getElementById('no-teacher-alerts');
const alertBadge      = document.getElementById('alert-count-badge');
const sumTotal        = document.getElementById('sum-total');
const sumEngaged      = document.getElementById('sum-engaged');
const sumAlert        = document.getElementById('sum-alert');
const toggleCamBtn    = document.getElementById('btn-toggle-cam');
const toggleMicBtn    = document.getElementById('btn-toggle-mic');

// ── WebRTC helpers ────────────────────────────────────────────────────────────
async function createPeerForStudent(studentSid) {
  if (peerConns[studentSid]) {
    peerConns[studentSid].close();
  }

  const pc = new RTCPeerConnection(ICE_CONFIG);
  peerConns[studentSid] = pc;

  // Add teacher's tracks so student can see/hear teacher
  if (localStream) {
    localStream.getTracks().forEach(track => pc.addTrack(track, localStream));
  }

  // Receive student's video/audio tracks → put in their card video element
  pc.ontrack = (event) => {
    const videoEl = document.getElementById(`video-${studentSid}`);
    if (event.streams && event.streams[0]) {
      remoteStreams[studentSid] = event.streams[0];
    }
    if (videoEl && remoteStreams[studentSid]) {
      videoEl.srcObject = remoteStreams[studentSid];
      videoEl.muted = true;
      videoEl.play().catch(() => {});
    }
  };

  // ICE candidate relay
  pc.onicecandidate = (event) => {
    if (event.candidate) {
      socket.emit('webrtc_ice', {
        target_sid: studentSid,
        candidate:  event.candidate,
      });
    }
  };

  pc.onconnectionstatechange = () => {
    const state = pc.connectionState;
    if (state === 'connected') {
      updateCardStatus(studentSid, 'connected');
    } else if (state === 'failed' || state === 'disconnected') {
      updateCardStatus(studentSid, 'away');
    }
  };

  // Teacher creates offer
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);

  socket.emit('webrtc_offer', {
    target_sid: studentSid,
    sdp:        offer,
  });

  return pc;
}

// ── Camera / mic ──────────────────────────────────────────────────────────────
async function startLocalMedia() {
  try {
    localStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
    localVideo.srcObject = localStream;
  } catch (err) {
    console.warn('Teacher camera error:', err.message);
    alert('Cannot access camera/mic: ' + err.message);
  }
}

// ── Socket events ─────────────────────────────────────────────────────────────
socket.on('connect', async () => {
  await startLocalMedia();
  socket.emit('teacher_join', { meeting_code: MEETING_CODE });
});

socket.on('meeting_state', (data) => {
  // Reconnect: restore existing students
  Object.entries(data.students || {}).forEach(([sid, info]) => {
    students[sid] = info;
    renderCard(sid);
    createPeerForStudent(sid);
  });
  updateSummary();
});

socket.on('student_joined', async (data) => {
  students[data.sid] = data;
  renderCard(data.sid);
  updateSummary();
  showToast(`${data.name} joined`);
  // Teacher initiates WebRTC with the new student
  await createPeerForStudent(data.sid);
});

// Student sent answer to our offer
socket.on('webrtc_answer', async (data) => {
  const pc = peerConns[data.from_sid];
  if (pc) {
    await pc.setRemoteDescription(new RTCSessionDescription(data.sdp));
  }
});

// ICE candidate from a student
socket.on('webrtc_ice', async (data) => {
  const pc = peerConns[data.from_sid];
  if (pc && data.candidate) {
    try {
      await pc.addIceCandidate(new RTCIceCandidate(data.candidate));
    } catch (e) { console.warn('ICE err', e); }
  }
});

socket.on('student_left', (data) => {
  const { sid, name } = data;
  if (peerConns[sid]) { peerConns[sid].close(); delete peerConns[sid]; }
  delete remoteStreams[sid];
  delete students[sid];
  document.getElementById(`card-${sid}`)?.remove();
  updateSummary();
  updateWaiting();
  showToast(`${name} left`);
});

socket.on('student_status_update', (data) => {
  if (!students[data.sid]) return;
  Object.assign(students[data.sid], data);
  updateCard(data.sid);
  updateSummary();
});

socket.on('student_alert', (data) => {
  addAlertLog(data);
  updateSummary();
});

socket.on('meeting_ended', () => {
  window.location.href = '/teacher';
});

// ── Student card rendering ────────────────────────────────────────────────────
const STATUS_LABELS = {
  engaged:   'Engaged',
  sleeping:  'Sleeping',
  away:      'Looking away',
  phone_usage: 'Phone usage',
  no_face:   'Not visible',
  unknown:   'Waiting…',
  connected: 'Connected',
  error:     'Error',
};

function dotClass(status) {
  return ({ engaged:'dot-engaged', sleeping:'dot-sleeping', away:'dot-away',
            phone_usage:'dot-phone', no_face:'dot-no_face', connected:'dot-connected' })[status] || 'dot-unknown';
}
function activeAlertType(student) {
  const activeStatuses = new Set(['sleeping', 'away', 'phone_usage', 'no_face']);
  return activeStatuses.has(student?.status) ? student.status : null;
}
function cardHighlight(alertType) {
  return ({ sleeping:'card-sleeping', away:'card-away', phone_usage:'card-phone', no_face:'card-no_face' })[alertType] || 'card-engaged';
}

function ensureCard(sid) {
  let card = document.getElementById(`card-${sid}`);
  if (!card) {
    card = document.createElement('div');
    card.id = `card-${sid}`;
    card.innerHTML = `
      <div class="sc-video-wrap">
        <video id="video-${sid}" autoplay playsinline muted class="sc-video"></video>
        <div class="sc-video-label"></div>
        <div class="sc-status-overlay"></div>
      </div>
      <div class="sc-info">
        <div class="sc-status">
          <span class="sc-dot"></span>
          <span class="sc-status-text"></span>
        </div>
        <div class="sc-ear"></div>
      </div>
    `;
    studentGrid.appendChild(card);
  }
  return card;
}

function updateCardUI(sid) {
  const s = students[sid];
  if (!s) return;

  const card = ensureCard(sid);

  const st  = s.status || 'connected';
  const activeAlert = activeAlertType(s);
  const ear = s.ear != null ? Number(s.ear).toFixed(3) : '—';

  card.className = `student-card ${cardHighlight(activeAlert)}`;
  const videoLabelEl = card.querySelector('.sc-video-label');
  const statusOverlayEl = card.querySelector('.sc-status-overlay');
  const statusDotEl = card.querySelector('.sc-dot');
  const statusTextEl = card.querySelector('.sc-status-text');
  const earEl = card.querySelector('.sc-ear');

  if (videoLabelEl) videoLabelEl.textContent = s.name;
  if (statusOverlayEl) {
    statusOverlayEl.textContent = activeAlert ? (STATUS_LABELS[st] || st) : '';
    statusOverlayEl.className = `sc-status-overlay ${activeAlert ? 'has-alert' : ''}`;
  }
  if (statusDotEl) statusDotEl.className = `sc-dot ${dotClass(st)}`;
  if (statusTextEl) statusTextEl.textContent = STATUS_LABELS[st] || st;
  if (earEl) earEl.textContent = `Eye: ${ear}`;

  updateStudentCount();
}

function attachRemoteStream(sid) {
  const videoEl = document.getElementById(`video-${sid}`);
  const stream = remoteStreams[sid];
  if (!videoEl || !stream) return;

  if (videoEl.srcObject !== stream) {
    videoEl.srcObject = stream;
  }
  videoEl.muted = true;
  videoEl.play().catch(() => {});
}

function renderCard(sid) {
  if (waitingMsg) waitingMsg.style.display = 'none';
  ensureCard(sid);
  updateCardUI(sid);
  attachRemoteStream(sid);
}

function updateCard(sid) {
  if (students[sid]) updateCardUI(sid);
}
function updateCardStatus(sid, status) {
  if (students[sid]) {
    students[sid].status = status;
    updateCardUI(sid);
  }
}

function updateWaiting() {
  if (Object.keys(students).length === 0 && waitingMsg) {
    waitingMsg.style.display = '';
  }
}
function updateStudentCount() {
  const n = Object.keys(students).length;
  studentCountEl.textContent = `${n} student${n !== 1 ? 's' : ''}`;
}

// ── Summary ───────────────────────────────────────────────────────────────────
function updateSummary() {
  const all = Object.values(students);
  sumTotal.textContent   = all.length;
  sumEngaged.textContent = all.filter(s => s.status === 'engaged').length;
  sumAlert.textContent   = all.filter(s => activeAlertType(s)).length;
}

// ── Alert log ─────────────────────────────────────────────────────────────────
function addAlertLog(data) {
  if (noAlertsEl) noAlertsEl.style.display = 'none';
  alertCount++;
  alertBadge.style.display = 'inline-flex';
  alertBadge.textContent   = alertCount;

  const item = document.createElement('div');
  item.className = `alert-item type-${data.alert_type || 'unknown'}`;
  item.innerHTML = `
    <div class="alert-head">
      <div class="alert-name">${data.name}</div>
      <button class="alert-dismiss" type="button" aria-label="Dismiss alert">Dismiss</button>
    </div>
    <div class="alert-msg">${data.alert}</div>
    <div class="alert-time">${data.time || ''}</div>
  `;
  item.querySelector('.alert-dismiss')?.addEventListener('click', () => {
    item.remove();
    alertCount = Math.max(0, alertCount - 1);
    alertBadge.textContent = alertCount;
    alertBadge.style.display = alertCount > 0 ? 'inline-flex' : 'none';
    if (!alertList.querySelector('.alert-item') && noAlertsEl) {
      noAlertsEl.style.display = '';
    }
  });
  alertList.prepend(item);
  showToast(`⚠ ${data.name}: ${data.alert}`, 4000);
}

// ── Controls ──────────────────────────────────────────────────────────────────
toggleCamBtn?.addEventListener('click', () => {
  isCamPaused = !isCamPaused;
  localStream?.getVideoTracks().forEach(t => { t.enabled = !isCamPaused; });
  toggleCamBtn.textContent = isCamPaused ? 'Resume Cam' : 'Pause Cam';
});

toggleMicBtn?.addEventListener('click', () => {
  isMuted = !isMuted;
  localStream?.getAudioTracks().forEach(t => { t.enabled = !isMuted; });
  toggleMicBtn.textContent = isMuted ? 'Unmute' : 'Mute';
  toggleMicBtn.classList.toggle('btn-danger', isMuted);
});

// ── Toast ─────────────────────────────────────────────────────────────────────
let toastTimer = null;
function showToast(msg, ms = 2500) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.style.display = 'none'; }, ms);
}
