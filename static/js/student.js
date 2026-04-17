/**
 * student.js
 * - Gets student camera/mic
 * - Connects to teacher via WebRTC (receives teacher video, sends student video)
 * - Sends frames for AI engagement analysis
 * - Updates status UI
 */

const socket    = io();
const ICE_CONFIG = {
  iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
};

// ── State ─────────────────────────────────────────────────────────────────────
let localStream   = null;    // student's camera+mic stream
let peerConn      = null;    // RTCPeerConnection with teacher
let teacherSid    = null;    // teacher's socket id
let captureTimer  = null;
let isCapturing   = true;
let isMuted       = false;
let isCamPaused   = false;
const CAPTURE_MS  = 1500;

// ── DOM ───────────────────────────────────────────────────────────────────────
const studentVideo = document.getElementById('student-video');   // own PiP
const teacherVideo = document.getElementById('teacher-video');   // teacher feed
const canvas       = document.getElementById('capture-canvas');
const ctx          = canvas.getContext('2d');
const statusPill   = document.getElementById('status-pill');
const overlayText  = document.getElementById('overlay-text');
const statusEl     = document.getElementById('engagement-status');
const earEl        = document.getElementById('ear-value');
const alertList    = document.getElementById('alert-list');
const noAlertsMsg  = document.getElementById('no-alerts-msg');
const toggleCamBtn = document.getElementById('btn-toggle-cam');
const toggleMicBtn = document.getElementById('btn-toggle-mic');

// ── Status labels ─────────────────────────────────────────────────────────────
const STATUS_LABELS = {
  engaged:   '✓ Engaged',
  sleeping:  '⚠ Sleeping',
  away:      '⚠ Looking away',
  phone_usage: '⚠ Possible phone usage',
  no_face:   '⚠ Not visible',
  unknown:   '— Unknown',
  connected: '● Connected',
  error:     '✗ Error',
};
const PILL_CLASS = {
  engaged:   'pill-engaged',
  sleeping:  'pill-sleeping',
  away:      'pill-away',
  phone_usage: 'pill-away',
  no_face:   'pill-alert',
};

function setStatusUI(status, ear) {
  const label = STATUS_LABELS[status] || status;
  statusPill.textContent = label;
  statusPill.className = 'room-status-pill ' + (PILL_CLASS[status] || '');
  statusEl.textContent = label;
  if (overlayText) {
    overlayText.textContent = (status === 'engaged' || status === 'connected') ? '' : label;
  }
  earEl.textContent = ear != null ? ear.toFixed(3) : '—';
}

// ── Camera / mic ──────────────────────────────────────────────────────────────
async function startLocalMedia() {
  try {
    localStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
    studentVideo.srcObject = localStream;
    return true;
  } catch (err) {
    statusPill.textContent = 'Camera error';
    alert('Cannot access camera/mic: ' + err.message);
    return false;
  }
}

// ── WebRTC helpers ────────────────────────────────────────────────────────────
function createPeerConnection(remoteSid) {
  const pc = new RTCPeerConnection(ICE_CONFIG);

  // Send our tracks to the teacher
  if (localStream) {
    localStream.getTracks().forEach(track => pc.addTrack(track, localStream));
  }

  // Receive teacher's tracks
  pc.ontrack = (event) => {
    if (event.streams && event.streams[0]) {
      teacherVideo.srcObject = event.streams[0];
    }
  };

  // Relay ICE candidates
  pc.onicecandidate = (event) => {
    if (event.candidate) {
      socket.emit('webrtc_ice', {
        target_sid: remoteSid,
        candidate:  event.candidate,
      });
    }
  };

  pc.onconnectionstatechange = () => {
    if (pc.connectionState === 'connected') {
      statusPill.textContent = 'Connected ✓';
      statusPill.className = 'room-status-pill pill-engaged';
    }
  };

  return pc;
}

// ── Socket events ─────────────────────────────────────────────────────────────
socket.on('connect', async () => {
  const ok = await startLocalMedia();
  if (!ok) return;

  socket.emit('student_join', {
    meeting_code: MEETING_CODE,
    name:         STUDENT_NAME,
  });
});

socket.on('joined_ok', () => {
  startCapture();
});

// Teacher is already in the room — wait for their offer (they will initiate)
socket.on('teacher_present', (data) => {
  teacherSid = data.host_sid;
  statusPill.textContent = 'Teacher connected';
});

// Teacher sends offer (they initiate for each student)
socket.on('webrtc_offer', async (data) => {
  teacherSid = data.from_sid;
  peerConn = createPeerConnection(teacherSid);

  await peerConn.setRemoteDescription(new RTCSessionDescription(data.sdp));
  const answer = await peerConn.createAnswer();
  await peerConn.setLocalDescription(answer);

  socket.emit('webrtc_answer', {
    target_sid: teacherSid,
    sdp:        answer,
  });
});

// ICE candidates from teacher
socket.on('webrtc_ice', async (data) => {
  if (peerConn && data.candidate) {
    try {
      await peerConn.addIceCandidate(new RTCIceCandidate(data.candidate));
    } catch (e) { console.warn('ICE error', e); }
  }
});

// AI analysis result back from server
socket.on('analysis_result', (result) => {
  setStatusUI(result.status, result.ear);
  if (result.alert) addAlert(result.alert, result.alert_type);
});

socket.on('meeting_ended', () => {
  stopCapture();
  alert('The teacher has ended this meeting.');
  window.location.href = '/student';
});

socket.on('teacher_left', () => {
  if (teacherVideo) teacherVideo.srcObject = null;
  statusPill.textContent = 'Teacher disconnected';
  statusPill.className = 'room-status-pill';
});

socket.on('disconnect', () => {
  stopCapture();
  statusPill.textContent = 'Disconnected';
});

// ── Frame capture for AI ──────────────────────────────────────────────────────
function captureAndSend() {
  if (!localStream || !isCapturing) return;
  const w = studentVideo.videoWidth;
  const h = studentVideo.videoHeight;
  if (!w || !h) return;

  canvas.width  = w;
  canvas.height = h;
  ctx.drawImage(studentVideo, 0, 0, w, h);
  const dataUrl = canvas.toDataURL('image/jpeg', 0.5);

  socket.emit('frame_analysis', {
    meeting_code: MEETING_CODE,
    frame:        dataUrl,
  });
}

function startCapture() {
  if (captureTimer) return;
  captureTimer = setInterval(captureAndSend, CAPTURE_MS);
}
function stopCapture() {
  clearInterval(captureTimer);
  captureTimer = null;
}

// ── Controls ──────────────────────────────────────────────────────────────────
toggleCamBtn.addEventListener('click', () => {
  isCamPaused = !isCamPaused;
  localStream.getVideoTracks().forEach(t => { t.enabled = !isCamPaused; });
  toggleCamBtn.textContent = isCamPaused ? 'Resume Cam' : 'Pause Cam';
  if (isCamPaused) stopCapture(); else startCapture();
});

toggleMicBtn.addEventListener('click', () => {
  isMuted = !isMuted;
  localStream.getAudioTracks().forEach(t => { t.enabled = !isMuted; });
  toggleMicBtn.textContent = isMuted ? 'Unmute' : 'Mute';
  toggleMicBtn.classList.toggle('btn-danger', isMuted);
});

// ── Alert panel ───────────────────────────────────────────────────────────────
const shownAlerts = new Set();

function addAlert(message, type) {
  const key = `${type}:${message}`;
  if (shownAlerts.has(key)) return;
  shownAlerts.add(key);
  setTimeout(() => shownAlerts.delete(key), 8000);

  if (noAlertsMsg) noAlertsMsg.style.display = 'none';
  const item = document.createElement('div');
  item.className = `alert-item type-${type || 'unknown'}`;
  const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  item.innerHTML = `<div class="alert-msg">${message}</div><div class="alert-time">${time}</div>`;
  alertList.prepend(item);
}
