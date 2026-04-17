import os
import random
import string
from datetime import datetime

from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room

from database import (init_db, create_user, authenticate_user,
                      create_meeting, get_meeting_by_code, get_meeting_any,
                      get_meetings_by_host, end_meeting,
                      save_report, get_reports_by_meeting, has_reports)
from engagement import EngagementDetector

app = Flask(__name__)
# Keep sessions stable across debug reloads during local development.
app.secret_key = os.environ.get('SECRET_KEY', 'classwatch-dev-secret')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet',
                    max_http_buffer_size=10 * 1024 * 1024)

# ── In-memory state ───────────────────────────────────────────────────────────
active_meetings: dict = {}   # code -> { host_sid, students: {sid: info} }
student_detectors: dict = {} # sid -> EngagementDetector
student_stats: dict = {}     # sid -> { total_frames, engaged_frames, sleeping_frames, ... }


def generate_code(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def login_required(role=None):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    if role and session.get('role') != role:
        return redirect(url_for('login'))
    return None

def init_student_stats(sid, joined_at=None):
    """Initialize engagement tracking stats for a student."""
    student_stats[sid] = {
        'total_frames':    0,
        'engaged_frames':  0,
        'sleeping_frames': 0,
        'away_frames':     0,
        'no_face_frames':  0,
        'total_alerts':    0,
        'sleeping_alerts': 0,
        'away_alerts':     0,
        'no_face_alerts':  0,
        'joined_at':       joined_at or datetime.now().strftime('%H:%M:%S'),
    }


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('teacher_home') if session.get('role') == 'teacher'
                        else url_for('student_home'))
    return render_template('index.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET' and 'user_id' in session:
        return redirect(url_for('teacher_home') if session.get('role') == 'teacher'
                        else url_for('student_home'))
    if request.method == 'POST':
        email    = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        user = authenticate_user(email, password)
        if user:
            session['user_id'] = user['id']
            session['name']    = user['name']
            session['email']   = user['email']
            session['role']    = user['role']
            pending = session.pop('pending_join', None)
            if pending:
                return redirect(url_for('join_meeting', code=pending))
            return redirect(url_for('teacher_home') if user['role'] == 'teacher'
                            else url_for('student_home'))
        return render_template('login.html', error='Invalid email or password.')
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name     = request.form.get('name', '').strip()
        email    = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        role     = request.form.get('role', 'student')
        user_id  = create_user(name, email, password, role)
        if user_id:
            return redirect(url_for('login'))
        return render_template('register.html', error='Email is already registered.')
    return render_template('register.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


# ── Teacher routes ────────────────────────────────────────────────────────────

@app.route('/teacher')
def teacher_home():
    err = login_required('teacher')
    if err: return err
    meetings = get_meetings_by_host(session['user_id'])
    # Check which meetings have reports
    for m in meetings:
        m['has_report'] = has_reports(m['code'])
    return render_template('teacher_home.html', meetings=meetings, user=session)


@app.route('/teacher/create', methods=['GET', 'POST'])
def create_meeting_route():
    err = login_required('teacher')
    if err: return err
    if request.method == 'POST':
        title = request.form.get('title', 'My Class').strip() or 'My Class'
        code  = generate_code()
        create_meeting(session['user_id'], title, code)
        return redirect(url_for('teacher_room', code=code))
    return render_template('create_meeting.html', user=session)


@app.route('/teacher/room/<code>')
def teacher_room(code):
    err = login_required('teacher')
    if err: return err
    meeting = get_meeting_by_code(code)
    if not meeting:
        return redirect(url_for('teacher_home'))
    host_url = request.host_url
    return render_template('teacher_room.html', meeting=meeting,
                           user=session, code=code, host_url=host_url)


@app.route('/teacher/end/<code>', methods=['POST'])
def end_meeting_route(code):
    err = login_required('teacher')
    if err: return err

    # Save reports for all students in this meeting before ending
    meeting = active_meetings.get(code)
    if meeting:
        for sid, info in meeting.get('students', {}).items():
            stats = student_stats.get(sid, {})
            stats['left_at'] = datetime.now().strftime('%H:%M:%S')
            if not stats.get('joined_at'):
                stats['joined_at'] = info.get('joined_at', '')
            save_report(code, info['name'], stats)
            # Cleanup student stats
            student_stats.pop(sid, None)
            student_detectors.pop(sid, None)

    end_meeting(code)

    # Notify only students. The teacher already gets the HTTP redirect to the
    # report page, so emitting to the whole meeting room causes conflicting
    # client-side redirects during teardown.
    for sid in list(meeting.get('students', {}).keys()) if meeting else []:
        socketio.emit('meeting_ended', {}, room=sid)

    active_meetings.pop(code, None)
    return redirect(url_for('meeting_report', code=code))


@app.route('/teacher/report/<code>')
def meeting_report(code):
    err = login_required('teacher')
    if err: return err
    meeting = get_meeting_any(code)
    if not meeting:
        return redirect(url_for('teacher_home'))
    reports = get_reports_by_meeting(code)

    # Compute meeting-level summary
    summary = {
        'total_students': len(reports),
        'avg_engagement': 0.0,
        'total_alerts':   0,
        'best_student':   None,
        'worst_student':  None,
    }
    if reports:
        scores = [r['engagement_score'] for r in reports]
        summary['avg_engagement'] = round(sum(scores) / len(scores), 1)
        summary['total_alerts'] = sum(r['total_alerts'] for r in reports)
        summary['best_student'] = max(reports, key=lambda r: r['engagement_score'])
        summary['worst_student'] = min(reports, key=lambda r: r['engagement_score'])

    return render_template('report.html', meeting=meeting, reports=reports,
                           summary=summary, user=session, code=code)


# ── Student routes ────────────────────────────────────────────────────────────

@app.route('/student')
def student_home():
    err = login_required('student')
    if err: return err
    return render_template('student_home.html', user=session)


@app.route('/join/<code>')
def join_meeting(code):
    if 'user_id' not in session:
        session['pending_join'] = code
        return redirect(url_for('login'))
    meeting = get_meeting_by_code(code)
    if not meeting:
        return render_template('student_home.html', user=session,
                               error='Meeting not found or has ended.')
    return render_template('student_room.html', meeting=meeting,
                           user=session, code=code)


@app.route('/join', methods=['POST'])
def join_by_code():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    code = request.form.get('code', '').strip().upper()
    if code:
        return redirect(url_for('join_meeting', code=code))
    return render_template('student_home.html', user=session,
                           error='Please enter a meeting code.')


# ── Socket.IO — Meeting management ────────────────────────────────────────────

@socketio.on('teacher_join')
def on_teacher_join(data):
    code = data.get('meeting_code')
    if not code: return
    if code not in active_meetings:
        active_meetings[code] = {'host_sid': request.sid, 'students': {}}
    else:
        active_meetings[code]['host_sid'] = request.sid
    join_room(f'teacher_{code}')
    join_room(f'meeting_{code}')
    emit('meeting_state', {'students': active_meetings[code]['students']})


@socketio.on('student_join')
def on_student_join(data):
    code = data.get('meeting_code')
    name = data.get('name', 'Student')
    if not code: return

    if code not in active_meetings:
        active_meetings[code] = {'host_sid': None, 'students': {}}

    joined_at = datetime.now().strftime('%H:%M:%S')

    student_info = {
        'sid':        request.sid,
        'name':       name,
        'status':     'connected',
        'alert':      None,
        'alert_type': None,
        'ear':        None,
        'joined_at':  joined_at,
    }
    active_meetings[code]['students'][request.sid] = student_info

    if request.sid in student_detectors:
        student_detectors[request.sid].reset()
    else:
        student_detectors[request.sid] = EngagementDetector()

    # Initialize stats tracking for this student
    init_student_stats(request.sid, joined_at)

    join_room(f'meeting_{code}')

    # Tell teacher a new student arrived (teacher will initiate WebRTC)
    socketio.emit('student_joined', student_info, room=f'teacher_{code}')
    emit('joined_ok', {'code': code})

    # If teacher is already in the room, notify student so they wait for offer
    host_sid = active_meetings[code].get('host_sid')
    if host_sid:
        emit('teacher_present', {'host_sid': host_sid})


# ── Socket.IO — WebRTC Signaling Relay ───────────────────────────────────────
# Teacher → Student: offer
@socketio.on('webrtc_offer')
def on_webrtc_offer(data):
    """Teacher sends an offer to a specific student."""
    target_sid = data.get('target_sid')
    socketio.emit('webrtc_offer', {
        'sdp':        data.get('sdp'),
        'from_sid':   request.sid,
    }, room=target_sid)


# Student → Teacher: answer
@socketio.on('webrtc_answer')
def on_webrtc_answer(data):
    """Student sends answer back to teacher."""
    target_sid = data.get('target_sid')
    socketio.emit('webrtc_answer', {
        'sdp':          data.get('sdp'),
        'from_sid':     request.sid,
    }, room=target_sid)


# Both directions: ICE candidates
@socketio.on('webrtc_ice')
def on_webrtc_ice(data):
    """Relay ICE candidate to the target peer."""
    target_sid = data.get('target_sid')
    socketio.emit('webrtc_ice', {
        'candidate': data.get('candidate'),
        'from_sid':  request.sid,
    }, room=target_sid)


# ── Socket.IO — AI frame analysis ─────────────────────────────────────────────

@socketio.on('frame_analysis')
def on_frame(data):
    code       = data.get('meeting_code')
    frame_data = data.get('frame')
    if not frame_data or not code: return
    if request.sid not in student_detectors: return

    result  = student_detectors[request.sid].analyze_frame(frame_data)
    meeting = active_meetings.get(code)

    # ── Update engagement stats ───────────────────────────────────────
    if request.sid in student_stats:
        st = student_stats[request.sid]
        st['total_frames'] += 1
        status = result.get('status', 'unknown')
        if status == 'engaged':
            st['engaged_frames'] += 1
        elif status == 'sleeping':
            st['sleeping_frames'] += 1
        elif status == 'away':
            st['away_frames'] += 1
        elif status == 'no_face':
            st['no_face_frames'] += 1

    if meeting and request.sid in meeting['students']:
        s = meeting['students'][request.sid]
        previous_alert_type = s.get('alert_type')
        s['status']     = result.get('status', 'unknown')
        s['ear']        = result.get('ear')
        s['alert']      = result.get('alert')
        s['alert_type'] = result.get('alert_type')

        socketio.emit('student_status_update', {
            'sid':        request.sid,
            'name':       s['name'],
            'status':     result['status'],
            'ear':        result.get('ear'),
            'alert':      result.get('alert'),
            'alert_type': result.get('alert_type'),
        }, room=f'teacher_{code}')

        current_alert_type = result.get('alert_type')
        is_new_alert = current_alert_type and current_alert_type != previous_alert_type

        if is_new_alert:
            if request.sid in student_stats:
                st = student_stats[request.sid]
                st['total_alerts'] += 1
                if current_alert_type == 'sleeping':
                    st['sleeping_alerts'] += 1
                elif current_alert_type == 'away':
                    st['away_alerts'] += 1
                elif current_alert_type == 'no_face':
                    st['no_face_alerts'] += 1

            socketio.emit('student_alert', {
                'sid':        request.sid,
                'name':       s['name'],
                'alert':      result['alert'],
                'alert_type': current_alert_type,
                'status':     result['status'],
                'time':       datetime.now().strftime('%H:%M:%S'),
            }, room=f'teacher_{code}')

    emit('analysis_result', result)


# ── Socket.IO — Disconnect ────────────────────────────────────────────────────

@socketio.on('disconnect')
def on_disconnect():
    for code, meeting in list(active_meetings.items()):
        if request.sid in meeting['students']:
            info = meeting['students'].pop(request.sid)
            name = info['name']

            # Save report for disconnected student (left early)
            stats = student_stats.pop(request.sid, {})
            if stats.get('total_frames', 0) > 0:
                stats['left_at'] = datetime.now().strftime('%H:%M:%S')
                save_report(code, name, stats)

            student_detectors.pop(request.sid, None)
            socketio.emit('student_left', {'sid': request.sid, 'name': name},
                          room=f'teacher_{code}')
            break
        # If teacher disconnects
        if meeting.get('host_sid') == request.sid:
            meeting['host_sid'] = None
            socketio.emit('teacher_left', {}, room=f'meeting_{code}')


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    print(f'ClassWatch running on http://localhost:{port}')
    socketio.run(app, debug=True, host='0.0.0.0', port=port)
