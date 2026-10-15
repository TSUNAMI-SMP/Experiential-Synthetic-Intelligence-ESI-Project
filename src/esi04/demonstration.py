"""Turn explicitly recorded user actions into source-linked, bounded skills."""

from esi.models import uid, utc
from esi04.computer import crop_anchor
from esi04.computer_models import ComputerAction, Postcondition, Selector, Skill, SkillStep


def element_at(frame, x, y):
    elements = [e for e in frame.elements if e.enabled and e.bounds.contains(x, y) and not e.protected
                and (e.name or e.automation_id)]
    return min(elements, key=lambda e: e.bounds.width*e.bounds.height) if elements else None


class Demonstration:
    def __init__(self, hub, name, goal):
        if not hub.grant.demonstrate:
            raise PermissionError("実演の記録は停止しています。")
        if not name.strip() or not goal.strip():
            raise ValueError("実演の名前と目的を入力してください。")
        self.hub, self.name, self.goal = hub, name[:160], goal[:1000]
        self.generation = hub.grant.generation
        self.application = hub.latest.foreground.application if hub.latest else ""
        self.pending = None
        self.steps = []
        self.skipped = 0
        self.finished = False

    def event(self, event, before):
        if self.finished or before is None or before.generation != self.generation:
            return
        if before.foreground.application != self.application:
            self.skipped += 1
            return
        if event["kind"] == "edit" and self.pending and self.pending["event"]["kind"] == "edit":
            self.pending["event"]["at"] = event["at"]
            return
        if self.pending:
            # The supervisor normally flushes the previous step with its post-action capture first.
            self.skipped += 1
        self.pending = {"event": dict(event), "before": before.model_copy(deep=True)}
        self.hub.memory.pinned.add(before.id)

    def captured(self, after):
        if not self.pending or self.finished:
            return
        event, before = self.pending["event"], self.pending["before"]
        settle = 0.45 if event["kind"] == "edit" else 0.12
        if after.captured_at < event["at"]+settle:
            return
        if after.generation != self.generation or after.foreground.application != self.application:
            return
        self.pending = None
        values = {"kind": event["kind"], "frame_id": before.id}
        anchor_path, anchor_hash, offset = "", "", [0, 0]
        if event["kind"] == "click":
            x, y = event["x"], event["y"]
            if not before.physical.contains(x, y):
                self.skipped += 1
                return
            target = element_at(before, x, y)
            if target:
                values["selector"] = Selector(name=target.name, role=target.role,
                                               automation_id=target.automation_id)
            else:
                values["point"] = [round((x-before.physical.x)*1000/before.physical.width),
                                   round((y-before.physical.y)*1000/before.physical.height)]
                anchor_path, anchor_hash, offset = crop_anchor(self.hub.memory, before, x, y)
        elif event["kind"] == "edit":
            targets = [e for e in after.elements if e.focused and not e.protected and e.value
                       and (e.name or e.automation_id)]
            if len(targets) != 1:
                self.skipped += 1
                return
            target = targets[0]
            selector = Selector(name=target.name, role=target.role, automation_id=target.automation_id)
            values.update(kind="type", text=target.value[:2000], selector=selector, replace=True,
                          expected=Postcondition(kind="element_value", selector=selector, value=target.value[:2000]))
        elif event["kind"] == "key":
            values["keys"] = event["keys"]
        elif event["kind"] == "scroll":
            values["amount"] = max(-5, min(5, event["amount"]))
        else:
            self.skipped += 1
            return
        # Retain an observed, specific accessible state change when available.
        if "expected" not in values:
            old = {(e.automation_id, e.name, e.role): e for e in before.elements}
            changed = [e for e in after.elements if not e.protected and (e.name or e.automation_id)
                       and e.value and (e.automation_id, e.name, e.role) in old
                       and old[(e.automation_id, e.name, e.role)].value != e.value]
            if len(changed) == 1:
                e = changed[0]
                values["expected"] = Postcondition(kind="element_value", value=e.value,
                                                    selector=Selector(name=e.name, role=e.role, automation_id=e.automation_id))
        self.steps.append(SkillStep(action=ComputerAction(**values), before_frame=before.id, after_frame=after.id,
                                    anchor_path=anchor_path, anchor_sha256=anchor_hash, anchor_offset=offset))
        self.hub.memory.pinned.add(after.id)
        if len(self.steps) >= 80:
            self.finished = True

    def finish(self):
        self.finished = True
        if not self.steps:
            self.hub.memory.pinned.clear()
            raise ValueError("再現できる操作が記録されていません。対象アプリで操作してから記録を終了してください。")
        skill = Skill(id=uid(), name=self.name, user_goal=self.goal, application=self.application,
                      taught_at=utc(), steps=self.steps,
                      complete=not self.skipped and self.pending is None,
                      last_result=f"実演を記録。再現時の結果は未確認。読み取れなかった操作{self.skipped}件")
        self.hub.memory.save_skill(skill)
        self.hub.memory.pinned.clear()
        return skill
