"""Bounded autonomous attention, poses and expressions over authored Cubism parameters."""

import math
import time

from PySide6.QtGui import QCursor

from esi02.live2d import AudioLiveCanvas


def target_pose(state, elapsed, gaze=None):
    cognition, body = state.get("cognition", {}), state.get("body", {})
    intention, emotion = cognition.get("intention", "listen"), cognition.get("emotion", "neutral")
    strength = max(0, min(1, cognition.get("emotion_strength", 0.2)))
    sway = math.sin(elapsed*0.8)
    values = {"ParamAngleX": 0.6*sway, "ParamAngleY": 0.4*math.sin(elapsed*0.55),
              "ParamAngleZ": 0.5*sway, "ParamBodyAngleX": 0.5*sway, "ParamBodyAngleY": 0.0,
              "ParamBodyAngleZ": 0.3*sway, "ParamEyeBallX": 0.0, "ParamEyeBallY": 0.0,
              "ParamBrowLY": 0.0, "ParamBrowRY": 0.0, "ParamBrowLAngle": 0.0, "ParamBrowRAngle": 0.0,
              "ParamMouthForm": 0.0,
              "ParamArmLZ": 0.0, "ParamArmRZ": 0.0}
    if intention == "think":
        values.update(ParamAngleZ=4.0, ParamAngleY=-2.0, ParamEyeBallX=-0.25, ParamEyeBallY=-0.2)
    elif intention == "ask":
        values.update(ParamAngleZ=-2.5, ParamAngleY=1.5, ParamArmRZ=7.0)
    elif intention in {"inspect", "operate"}:
        values.update(ParamAngleY=-3.0, ParamBodyAngleX=1.5, ParamArmLZ=3.0, ParamArmRZ=5.0)
    elif intention in {"play", "enjoy"}:
        values.update(ParamBodyAngleZ=1.5*sway, ParamArmLZ=3.0+2*sway, ParamArmRZ=3.0-2*sway)
    elif intention == "rest" or body.get("activity") == "resting":
        values.update(ParamAngleY=-3.0, ParamAngleZ=2.0, ParamBodyAngleX=-1.0)
    if emotion == "pleased":
        values.update(ParamBrowLY=0.25*strength, ParamBrowRY=0.25*strength,
                      ParamMouthForm=0.7*strength)
    elif emotion in {"uncertain", "concerned"}:
        values.update(ParamBrowLY=0.15*strength, ParamBrowRY=-0.1*strength,
                      ParamBrowLAngle=-0.3*strength, ParamBrowRAngle=-0.3*strength, ParamMouthForm=-0.3*strength)
    elif emotion == "surprised":
        values.update(ParamBrowLY=0.55*strength, ParamBrowRY=0.55*strength)
    elif emotion == "tired":
        values.update(ParamBrowLY=-0.2*strength, ParamBrowRY=-0.2*strength, ParamAngleY=-2*strength)
    if gaze:
        x, y = max(-1, min(1, gaze[0])), max(-1, min(1, gaze[1]))
        values.update(ParamEyeBallX=x*0.65, ParamEyeBallY=y*0.5,
                      ParamAngleX=values["ParamAngleX"]+x*6, ParamAngleY=values["ParamAngleY"]+y*4)
    if not state.get("autonomous_motion", True):
        return {key: 0.0 for key in values}
    return values


class AutonomousCanvas(AudioLiveCanvas):
    def __init__(self, *args, **kwargs):
        self.state_provider = lambda: {}
        self.values = {}
        self.last_frame = time.monotonic()
        super().__init__(*args, **kwargs)

    def paintGL(self):
        if not self.model or not self.live2d:
            return
        now = time.monotonic()
        delta, self.last_frame = min(0.2, max(0, now-self.last_frame)), now
        state = self.state_provider()
        gaze = state.get("gaze")
        if state.get("cursor_tracking", False):
            local = self.mapFromGlobal(QCursor.pos())
            gaze = ((local.x()-self.width()/2)/max(1, self.width()/2),
                    -(local.y()-self.height()*0.2)/max(1, self.height()/2))
        self.live2d.clearBuffer(0, 0, 0, 0)
        self.model.Update()
        desired = target_pose(state, now-self.clock, gaze)
        alpha = 1-math.exp(-delta/0.3)
        for name, target in desired.items():
            value = self.values.get(name, 0)+(target-self.values.get(name, 0))*alpha
            self.values[name] = value
            if name in self.parameters:
                self.model.SetParamById(name, value)
        self.apply_blink_visibility()
        if "ParamMouthOpenY" in self.parameters:
            self.model.SetParamById("ParamMouthOpenY", self.meter.level())
        self.model.Draw()
