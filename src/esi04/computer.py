"""Revocable screen/PC grants, fresh-frame actions and replay of demonstrated skills."""

import hashlib
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.signal import fftconvolve

from esi.models import uid, utc
from esi02.desktop_preferences import write_json
from esi02.dialogue import Cancelled
from esi04.computer_models import ActionResult, ComputerAction, Frame, Skill


@dataclass(frozen=True)
class Grant:
    generation: int = 0
    screen: str = ""
    observe: bool = False
    control: bool = False
    demonstrate: bool = False
    move_avatar: bool = False
    application: str = ""
    pid: int = 0


class ComputerMemory:
    def __init__(self, root, limit_mb=512):
        self.root = Path(root)
        self.limit = limit_mb*1024*1024
        self.lock = threading.RLock()
        self.pinned = set()
        self.in_use = {}
        self.deleted_skills = set()
        self.root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def pin(self, identifier):
        with self.lock:
            self.in_use[identifier] = self.in_use.get(identifier, 0)+1
        try:
            yield
        finally:
            with self.lock:
                self.in_use[identifier] -= 1
                if self.in_use[identifier] == 0:
                    del self.in_use[identifier]

    def frames(self):
        result = []
        for path in self.root.glob("frames/*.json"):
            try:
                result.append(Frame.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(result, key=lambda f: f.observed_at)

    def save_frame(self, png, **metadata):
        with self.lock:
            identifier = uid()
            folder = self.root / "frames"
            folder.mkdir(exist_ok=True)
            path = folder / (identifier+".png")
            path.write_bytes(png)
            try:
                frame = Frame(id=identifier, path=str(path), sha256=hashlib.sha256(png).hexdigest(), **metadata)
                write_json(path.with_suffix(".json"), frame.model_dump())
                self.prune(keep={identifier})
            except Exception:
                path.unlink(missing_ok=True)
                path.with_suffix(".json").unlink(missing_ok=True)
                raise
            return frame

    def skills(self):
        result = []
        for path in (self.root / "skills").glob("*.json"):
            result.append(Skill.model_validate_json(path.read_text(encoding="utf-8")))
        return sorted(result, key=lambda s: s.taught_at)

    def save_skill(self, skill):
        with self.lock:
            skill = Skill.model_validate(skill.model_dump())
            if skill.id in self.deleted_skills:
                return
            folder = self.root / "skills"
            folder.mkdir(exist_ok=True)
            write_json(folder / (skill.id+".json"), skill.model_dump())

    def delete_skill(self, identifier):
        with self.lock:
            skills = self.skills()
            target = next((s for s in skills if s.id == identifier), None)
            if target is None:
                raise ValueError("その実演は見つかりません。")
            self.deleted_skills.add(identifier)
            path = self.root / "skills" / (target.id+".json")
            if path.resolve().parent != (self.root / "skills").resolve():
                raise ValueError("実演の保存先を確認できません。")
            path.unlink()
            retained = {step.anchor_path for skill in skills if skill.id != identifier for step in skill.steps}
            for step in target.steps:
                anchor = Path(step.anchor_path)
                if step.anchor_path and step.anchor_path not in retained and anchor.resolve().parent == (self.root / "anchors").resolve():
                    anchor.unlink(missing_ok=True)
            self.prune()

    def record(self, kind, record, maximum=256):
        with self.lock:
            folder = self.root / kind
            folder.mkdir(exist_ok=True)
            write_json(folder / (record.id+".json"), record.model_dump())
            paths = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime)
            for path in paths[:-maximum]:
                path.unlink()

    def prune(self, keep=()):
        protected = set(keep) | self.pinned | self.in_use.keys()
        for skill in self.skills():
            protected.update(step.before_frame for step in skill.steps)
            protected.update(step.after_frame for step in skill.steps)
        paths = sorted(self.root.glob("frames/*.png"), key=lambda p: p.stat().st_mtime)
        def size(path):
            metadata = path.with_suffix(".json")
            return path.stat().st_size+(metadata.stat().st_size if metadata.is_file() else 0)
        total = sum(size(p) for p in paths)
        for path in paths:
            if total <= self.limit:
                break
            if path.stem not in protected:
                total -= size(path)
                path.unlink()
                path.with_suffix(".json").unlink(missing_ok=True)
        if total > self.limit:
            raise RuntimeError("保存した実演の画像が画面保存の上限に達しました。不要な実演を削除してください。")

    def erase(self):
        """Called only by the explicit screen-history deletion control, after revocation."""
        with self.lock:
            for folder in ("frames", "skills", "anchors", "observations", "actions", "teachings"):
                for path in (self.root / folder).glob("*"):
                    if path.is_file():
                        path.unlink()
            self.pinned.clear()


def crop_anchor(memory, frame, x, y):
    """Store a real image patch around the demonstrated point, not a model-created target."""
    with Image.open(frame.path) as image:
        ix = round((x-frame.physical.x)*image.width/frame.physical.width)
        iy = round((y-frame.physical.y)*image.height/frame.physical.height)
        box = (max(0, ix-65), max(0, iy-40), min(image.width, ix+65), min(image.height, iy+40))
        patch = image.crop(box)
        folder = memory.root / "anchors"
        folder.mkdir(exist_ok=True)
        path = folder / (uid()+".png")
        patch.save(path)
    return str(path), hashlib.sha256(path.read_bytes()).hexdigest(), [ix-box[0], iy-box[1]]


def match_anchor(frame, path, digest, offset):
    data = Path(path).read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("実演の目印が変更されています。")
    with Image.open(path) as source, Image.open(frame.path) as current:
        template = np.asarray(source.convert("L"), dtype=np.float64)
        image = np.asarray(current.convert("L"), dtype=np.float64)
    h, w = template.shape
    if h > image.shape[0] or w > image.shape[1] or template.std() < 5:
        raise ValueError("この画面では実演の目印を確実に探せません。もう一度見せてください。")
    n, mean = h*w, template.mean()
    kernel = np.ones((h, w))
    sums = fftconvolve(image, kernel, mode="valid")
    sums2 = fftconvolve(image*image, kernel, mode="valid")
    numerator = fftconvolve(image, (template-mean)[::-1, ::-1], mode="valid")
    denominator = np.sqrt(np.maximum(1e-6, sums2-sums*sums/n)*np.square(template-mean).sum())
    scores = numerator/denominator
    y, x = np.unravel_index(scores.argmax(), scores.shape)
    best = float(scores[y, x])
    other = scores.copy()
    other[max(0, y-h//2):y+h//2+1, max(0, x-w//2):x+w//2+1] = -1
    if best < 0.94 or (other.size > 1 and float(other.max()) > best-0.035):
        raise ValueError("実演の目印が見つからないか、同じ目印が複数あります。操作を止めました。")
    return frame.image_point((x+offset[0])*1000/frame.image_width, (y+offset[1])*1000/frame.image_height)


class ComputerHub:
    """Qt publishes captures; workers can only act against the current, revocable grant."""

    def __init__(self, memory, native):
        self.memory, self.native = memory, native
        self.condition = threading.Condition(threading.RLock())
        self.operation_lock = threading.Lock()
        self.grant = Grant()
        self.latest = None
        self.recent = {}
        self.sequence = 0
        self.attention = None
        self.active_cancel = None

    def configure(self, screen, observe=False, control=False, demonstrate=False, move_avatar=False,
                  application="", pid=0):
        with self.condition:
            self.revoke()
            self.grant = Grant(self.grant.generation, screen, bool(observe or control or demonstrate),
                               bool(control), bool(demonstrate), bool(move_avatar and control), application, pid)
            self.condition.notify_all()
            return self.grant

    def revoke(self):
        with self.condition:
            if self.active_cancel:
                self.active_cancel.set()
            self.grant = Grant(generation=self.grant.generation+1)
            self.latest = None
            self.recent.clear()
            self.attention = None
            self.condition.notify_all()

    def publish(self, frame):
        with self.condition:
            if not self.grant.observe or frame.generation != self.grant.generation or frame.screen != self.grant.screen:
                return False
            if self.grant.application and frame.foreground.application != self.grant.application:
                self.latest = None
                return False
            if self.grant.pid and frame.foreground.pid != self.grant.pid:
                self.latest = None
                return False
            self.sequence = frame.sequence
            self.latest = frame.model_copy(deep=True)
            self.recent[frame.id] = self.latest
            self.recent = dict(list(self.recent.items())[-48:])
            self.condition.notify_all()
            return True

    def snapshot(self, cancellation, after=None, timeout=8, generation=None, captured_after=None):
        deadline = time.monotonic()+timeout
        with self.condition:
            generation = self.grant.generation if generation is None else generation
            while True:
                cancellation.check()
                if not self.grant.observe or self.grant.generation != generation:
                    raise PermissionError("画面へのアクセスは停止しています。")
                frame = self.latest
                if (frame is not None and time.monotonic()-frame.captured_at <= 3
                        and (after is None or frame.sequence > after)
                        and (captured_after is None or frame.captured_at > captured_after)):
                    return frame.model_copy(deep=True)
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("許可した画面の新しい画像が届きません。画面・アプリの選択を確認してください。")
                self.condition.wait(min(0.15, remaining))

    def _check_control(self, frame, generation, cancellation):
        cancellation.check()
        grant = self.grant
        if not grant.control or grant.generation != generation:
            raise PermissionError("PC操作は停止しています。")
        foreground = self.native.foreground()
        if foreground.handle != frame.foreground.handle or foreground.application != frame.foreground.application:
            raise RuntimeError("操作直前にアプリが切り替わりました。再観察が必要です。")
        if grant.pid and foreground.pid != grant.pid:
            raise PermissionError("許可したアプリの外では操作しません。")
        if grant.application and foreground.application != grant.application:
            raise PermissionError("許可したアプリの外では操作しません。")
        return grant

    def _source(self, action, before):
        source = self.recent.get(action.frame_id)
        if source is None:
            path = self.memory.root / "frames" / (action.frame_id+".json")
            if path.is_file():
                source = Frame.model_validate_json(path.read_text(encoding="utf-8"))
        if source is None or source.generation != before.generation:
            raise ValueError("操作の根拠にした画面が古くなりました。もう一度観察してください。")
        if source.foreground.application != before.foreground.application:
            raise ValueError("操作の対象アプリが違います。")
        if source.foreground.handle != before.foreground.handle:
            raise ValueError("操作を考えている間に対象ウィンドウが変わりました。再観察してください。")
        return source

    def _target(self, action, before):
        source = self._source(action, before)
        if action.selector:
            matches = [e for e in before.elements if e.enabled and action.selector.matches(e)]
            if len(matches) != 1 or matches[0].protected:
                raise ValueError("操作対象が一つに決まらないか、保護された入力欄です。")
            return matches[0].bounds.center
        if source.sha256 == before.sha256 and source.physical == before.physical:
            return before.image_point(*action.point)
        point = source.image_point(*action.point)
        path, digest, offset = crop_anchor(self.memory, source, *point)
        try:
            return match_anchor(before, path, digest, offset)
        finally:
            Path(path).unlink(missing_ok=True)

    @staticmethod
    def condition_met(condition, frame):
        if condition is None:
            return False
        if condition.kind == "window_title":
            return bool(condition.value and condition.value in frame.foreground.title)
        matches = [e for e in frame.elements if condition.selector and condition.selector.matches(e) and not e.protected]
        if condition.kind == "element_visible":
            return len(matches) == 1
        if condition.kind == "element_value":
            return len(matches) == 1 and matches[0].value == condition.value
        # OCR/visual postconditions are checked by the visual worker, never assumed here.
        return False

    def act(self, action, cancellation):
        action = ComputerAction.model_validate(action)
        with self.operation_lock:
            generation = self.grant.generation
            before = self.snapshot(cancellation, generation=generation)
            source = self._source(action, before)
            target = self._target(action, before) if action.kind in {"click", "double_click", "move"} or action.selector else None
            if action.kind in {"type", "key"} and not action.selector:
                prior_focus = [(e.automation_id or e.name, e.role) for e in source.elements if e.focused]
                current_focus = [(e.automation_id or e.name, e.role) for e in before.elements if e.focused]
                if prior_focus != current_focus:
                    raise ValueError("操作を考えている間に入力先が変わりました。再観察してください。")
            with self.condition:
                self._check_control(before, generation, cancellation)
                if target and not before.physical.contains(*target):
                    raise ValueError("選択した画面の外です。")
                if action.kind in {"type", "key"} and any(e.protected and e.focused for e in before.elements):
                    raise PermissionError("パスワード入力欄への入力は行いません。")
                if action.kind in {"type", "key"} and any(e.focused and not before.physical.contains(*e.bounds.center)
                                                          for e in before.elements):
                    raise PermissionError("入力先が選択した画面の外にあるため止めました。")
                self.active_cancel = cancellation
                self.attention = {"generation": generation, "frame_id": before.id, "point": target,
                                  "label": action.selector.name if action.selector else action.kind,
                                  "at": time.monotonic()}
            try:
                # Recheck before every character so revocation interrupts even a long text input.
                if action.kind == "type":
                    if action.replace and not action.selector:
                        raise ValueError("入力欄を指定してから文章を置き換えてください。")
                    if action.selector:
                        with self.condition:
                            self._check_control(before, generation, cancellation)
                            self.native.click(*target)
                            if action.replace:
                                self.native.key(["CTRL", "A"])
                    for char in action.text:
                        with self.condition:
                            self._check_control(before, generation, cancellation)
                            self.native.type(char)
                        cancellation.event.wait(0.012)
                else:
                    with self.condition:
                        self._check_control(before, generation, cancellation)
                        if action.kind in {"click", "double_click"}:
                            self.native.click(*target, double=action.kind == "double_click")
                        elif action.kind == "move":
                            self.native.move(*target)
                        elif action.kind == "key":
                            self.native.key(action.keys)
                        elif action.kind == "scroll":
                            self.native.scroll(action.amount)
                completed_at = time.monotonic()
                cancellation.event.wait(0.25)
                after = self.snapshot(cancellation, after=before.sequence, generation=generation,
                                      captured_after=completed_at)
                verified = self.condition_met(action.expected, after)
                changed_condition = verified and not self.condition_met(action.expected, before)
                if action.kind == "move" and target:
                    verified = abs(after.cursor[0]-target[0]) <= 2 and abs(after.cursor[1]-target[1]) <= 2
                    changed_condition = verified and before.cursor != after.cursor
                result = ActionResult(id=uid(), action=action, before=before.id, after=after.id,
                                      status="verified" if verified else "changed_unverified" if after.sha256 != before.sha256 else "unchanged",
                                      detail="指定した結果を画面の要素情報で確認" if verified else "入力は実行したが、結果の成立は未確認",
                                      observed_at=utc(), effect_observed=bool(changed_condition))
                self.memory.record("actions", result)
                return result, after
            finally:
                self.active_cancel = None

    def replay(self, skill, cancellation):
        skill = Skill.model_validate(skill)
        generation = self.grant.generation
        results = []
        try:
            if not self.grant.control:
                raise PermissionError("PC操作を許可してから実演を再現してください。")
            if not skill.complete:
                raise ValueError("読み取れなかった操作がある実演です。対象アプリでゆっくり操作を見せ直してください。")
            for step in skill.steps:
                before = self.snapshot(cancellation, generation=generation)
                if before.foreground.application != skill.application:
                    raise ValueError("実演と対象アプリが違います。")
                action = step.action.model_copy(deep=True)
                action.frame_id = before.id
                if action.kind in {"click", "double_click", "move"} and not action.selector:
                    if not step.anchor_path:
                        raise ValueError("実演の対象を探す目印がありません。")
                    x, y = match_anchor(before, step.anchor_path, step.anchor_sha256, step.anchor_offset)
                    action.point = [round((x-before.physical.x)*1000/before.physical.width),
                                    round((y-before.physical.y)*1000/before.physical.height)]
                result, after = self.act(action.model_dump(), cancellation)
                results.append(result)
                # Intermediate clicks may have no provable effect. Explicit failed conditions stop replay.
                if action.expected and result.status != "verified":
                    raise RuntimeError("実演の途中結果を確認できないため、後の操作を止めました。")
            skill.uses += 1
            final_verified = bool(results and results[-1].status == "verified")
            skill.verified_uses += int(final_verified)
            skill.last_result = "最終結果を確認" if final_verified else "実行したが、最終結果は未確認"
            self.memory.save_skill(skill)
            return results
        except (ValueError, RuntimeError, PermissionError, OSError, Cancelled) as exc:
            skill.failures += 1
            skill.last_result = str(exc)[:1000]
            self.memory.save_skill(skill)
            raise
