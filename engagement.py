import base64
import os
import time
from collections import deque

os.environ.setdefault("MEDIAPIPE_DISABLE_GPU", "1")

import cv2
import mediapipe as mp
import numpy as np
from scipy.spatial import distance


class SustainedState:
    """Simple hold timer so alerts are time-based, not frame-rate based."""

    def __init__(self, hold_sec, clear_sec=0.5):
        self.hold_sec = hold_sec
        self.clear_sec = clear_sec
        self._onset = None
        self._offset = None
        self.active = False

    def update(self, condition):
        now = time.time()
        if condition:
            self._offset = None
            if self._onset is None:
                self._onset = now
            if (now - self._onset) >= self.hold_sec:
                self.active = True
        else:
            self._onset = None
            if self.active:
                if self._offset is None:
                    self._offset = now
                if (now - self._offset) >= self.clear_sec:
                    self.active = False
                    self._offset = None
            else:
                self._offset = None
        return self.active

    def reset(self):
        self._onset = None
        self._offset = None
        self.active = False


class EngagementDetector:
    """
    Browser-frame based engagement detector.

    Safe subset integrated from the user's script:
    - eye closure / sleepy detection
    - head-pose based looking away
    - no-face detection

    It intentionally avoids loading external task/model files so the site keeps
    working in the current project without extra assets.
    """

    LEFT_EYE_IDX = [362, 385, 387, 263, 373, 380]
    RIGHT_EYE_IDX = [33, 160, 158, 133, 153, 144]

    NOSE_TIP = 1
    CHIN = 152
    LEFT_EAR = 234
    RIGHT_EAR = 454
    LEFT_MOUTH = 61
    RIGHT_MOUTH = 291
    FOREHEAD = 10

    def __init__(self):
        self.face_mesh = mp.solutions.face_mesh.FaceMesh(
            max_num_faces=1,
            refine_landmarks=True,
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        )

        self.ear_threshold = 0.20
        self.yaw_threshold = 22.0
        self.sleep_hold_sec = 4.0
        self.away_hold_sec = 5.0
        self.phone_hold_sec = 0.65
        self.no_face_hold_sec = 3.0
        self.clear_grace_sec = 0.6
        self.phone_conf_threshold = 0.16
        self.phone_class_id = 67  # COCO "cell phone"
        self.phone_model_path = os.environ.get("CLASSWATCH_YOLO_MODEL", "/tmp/antg-models/yolov8n.pt")

        self.blink_history = deque(maxlen=15)
        self.sleep_event = SustainedState(self.sleep_hold_sec, clear_sec=self.clear_grace_sec)
        self.away_event = SustainedState(self.away_hold_sec, clear_sec=0.8)
        self.phone_event = SustainedState(self.phone_hold_sec, clear_sec=1.0)
        self.no_face_event = SustainedState(self.no_face_hold_sec, clear_sec=0.5)
        self.phone_model = self._load_phone_model()

    def _load_phone_model(self):
        """Load YOLO only if the local weights file is available."""
        if not os.path.exists(self.phone_model_path):
            return None
        try:
            from ultralytics import YOLO
            return YOLO(self.phone_model_path)
        except Exception:
            return None

    def _compute_ear(self, landmarks, indices, width, height):
        pts = [(int(landmarks[i].x * width), int(landmarks[i].y * height)) for i in indices]
        a = distance.euclidean(pts[1], pts[5])
        b = distance.euclidean(pts[2], pts[4])
        c = distance.euclidean(pts[0], pts[3])
        return (a + b) / (2.0 * c) if c > 0 else 0.0

    def _decode_frame(self, frame_data):
        if isinstance(frame_data, str):
            if "," in frame_data:
                frame_data = frame_data.split(",", 1)[1]
            raw = base64.b64decode(frame_data)
        else:
            raw = frame_data
        arr = np.frombuffer(raw, np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)

    def _lm_px(self, face, idx, width, height):
        return (face[idx].x * width, face[idx].y * height)

    def _is_face_visible(self, face):
        """Wide visibility bounds so natural down-looking poses still count."""
        try:
            key_points = [
                face[self.NOSE_TIP],
                face[self.LEFT_EAR],
                face[self.RIGHT_EAR],
                face[self.CHIN],
                face[self.FOREHEAD],
            ]
            valid = [(p.x, p.y) for p in key_points if 0.0 <= p.x <= 1.0 and 0.0 <= p.y <= 1.0]
            if len(valid) < 3:
                return False
            xs = [p[0] for p in valid]
            ys = [p[1] for p in valid]
            return (max(xs) - min(xs)) > 0.02 and (max(ys) - min(ys)) > 0.02
        except (IndexError, AttributeError):
            return False

    def _head_pose_angles(self, face, width, height):
        """
        Approximate head pose using solvePnP and a small set of landmarks.
        Falls back to zeros if pose cannot be estimated.
        """
        model_pts = np.array([
            [0.0, 0.0, 0.0],
            [0.0, -63.6, -12.5],
            [-43.3, 32.7, -26.0],
            [43.3, 32.7, -26.0],
            [-28.9, -28.9, -24.1],
            [28.9, -28.9, -24.1],
        ], dtype=np.float64)

        try:
            idx = [self.NOSE_TIP, self.CHIN, self.LEFT_EAR, self.RIGHT_EAR, self.LEFT_MOUTH, self.RIGHT_MOUTH]
            image_pts = np.array(
                [[face[i].x * width, face[i].y * height] for i in idx],
                dtype=np.float64,
            )
            cam = np.array(
                [[float(width), 0, width / 2], [0, float(width), height / 2], [0, 0, 1]],
                dtype=np.float64,
            )
            ok, rvec, tvec = cv2.solvePnP(
                model_pts,
                image_pts,
                cam,
                np.zeros((4, 1), dtype=np.float64),
                flags=cv2.SOLVEPNP_ITERATIVE,
            )
            if not ok:
                return 0.0, 0.0, 0.0
            rot, _ = cv2.Rodrigues(rvec)
            euler = cv2.decomposeProjectionMatrix(np.hstack([rot, tvec]))[6].flatten()
            return float(euler[0]), float(euler[1]), float(euler[2])
        except Exception:
            return 0.0, 0.0, 0.0

    def _detect_phone_visible(self, frame, width, height):
        """Run YOLO cell-phone detection with conservative filtering."""
        if self.phone_model is None:
            return False

        try:
            results = self.phone_model.predict(
                source=frame,
                classes=[self.phone_class_id],
                conf=self.phone_conf_threshold,
                verbose=False,
                imgsz=640,
                max_det=3,
            )
            if not results:
                return False

            boxes = getattr(results[0], "boxes", None)
            if boxes is None or len(boxes) == 0:
                return False

            frame_area = float(width * height)
            for box in boxes:
                coords = box.xyxy[0].tolist()
                x1, y1, x2, y2 = coords
                bw = max(0.0, x2 - x1)
                bh = max(0.0, y2 - y1)
                area_ratio = (bw * bh) / frame_area if frame_area else 0.0
                center_y_ratio = ((y1 + y2) / 2.0) / float(height)

                # Ignore tiny false positives and favor phones visible in the
                # lower/mid frame where a hand-held device usually appears.
                if area_ratio >= 0.001 and center_y_ratio >= 0.10:
                    return True
            return False
        except Exception:
            return False

    def _base_result(self):
        return {
            "face_detected": False,
            "sleeping": False,
            "looking_away": False,
            "phone_usage": False,
            "ear": None,
            "status": "unknown",
            "alert": None,
            "alert_type": None,
        }

    def analyze_frame(self, frame_data):
        result = self._base_result()

        try:
            frame = self._decode_frame(frame_data)
            if frame is None:
                result["status"] = "error"
                return result

            height, width = frame.shape[:2]
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            processed = self.face_mesh.process(rgb)

            if not processed.multi_face_landmarks:
                self.sleep_event.update(False)
                self.away_event.update(False)
                self.phone_event.update(False)
                if self.no_face_event.update(True):
                    result["looking_away"] = True
                    result["status"] = "no_face"
                    result["alert"] = "Student not visible — face not detected"
                    result["alert_type"] = "no_face"
                return result

            face = processed.multi_face_landmarks[0].landmark
            if not self._is_face_visible(face):
                self.sleep_event.update(False)
                self.away_event.update(False)
                self.phone_event.update(False)
                if self.no_face_event.update(True):
                    result["looking_away"] = True
                    result["status"] = "no_face"
                    result["alert"] = "Student not visible — face not detected"
                    result["alert_type"] = "no_face"
                return result

            self.no_face_event.update(False)
            result["face_detected"] = True

            left_ear = self._compute_ear(face, self.LEFT_EYE_IDX, width, height)
            right_ear = self._compute_ear(face, self.RIGHT_EYE_IDX, width, height)
            avg_ear = (left_ear + right_ear) / 2.0
            result["ear"] = round(avg_ear, 3)

            both_closed = left_ear < self.ear_threshold * 0.82 and right_ear < self.ear_threshold * 0.82
            self.blink_history.append(both_closed)
            recent_blinks = sum(1 for b in self.blink_history if b)
            drowsy_now = avg_ear < self.ear_threshold and recent_blinks >= 3
            sleeping_now = both_closed or drowsy_now

            if self.sleep_event.update(sleeping_now):
                self.away_event.update(False)
                self.phone_event.update(False)
                result["sleeping"] = True
                result["status"] = "sleeping"
                result["alert"] = "😴 Student appears sleepy or drowsy"
                result["alert_type"] = "sleeping"
                return result

            pitch, yaw, _roll = self._head_pose_angles(face, width, height)
            nose_x = face[self.NOSE_TIP].x
            nose_y = face[self.NOSE_TIP].y

            phone_visible_now = self._detect_phone_visible(frame, width, height)

            if self.phone_event.update(phone_visible_now):
                self.away_event.update(False)
                result["phone_usage"] = True
                result["status"] = "phone_usage"
                result["alert"] = "📱 Phone visible in the camera frame"
                result["alert_type"] = "phone_usage"
                return result

            off_center = nose_x < 0.22 or nose_x > 0.78 or nose_y < 0.14 or nose_y > 0.86
            looking_away_now = abs(yaw) > self.yaw_threshold or off_center

            if self.away_event.update(looking_away_now):
                result["looking_away"] = True
                result["status"] = "away"
                result["alert"] = "👀 Student is looking away from the screen"
                result["alert_type"] = "away"
                return result

            result["status"] = "engaged"
            return result

        except Exception as exc:
            result["status"] = "error"
            result["alert"] = f"Analysis error: {exc}"
            return result

    def reset(self):
        self.blink_history.clear()
        self.sleep_event.reset()
        self.away_event.reset()
        self.phone_event.reset()
        self.no_face_event.reset()
