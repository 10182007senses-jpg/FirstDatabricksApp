"""ルール判定・割り当て作成・連絡文・データアクセスのテスト(SQLite で実行)。"""
from datetime import date

import pytest

from scheduler import llm, manager, messages, reasons, rules, sample_data, solver
from scheduler.data_access import Repo, SqliteBackend
from scheduler.models import Assignment, slot_id_of

MONTH = "2026-12"  # 日曜は 6, 13, 20, 27 日
D1, D2, D3, D4 = date(2026, 12, 6), date(2026, 12, 13), date(2026, 12, 20), date(2026, 12, 27)


@pytest.fixture
def repo():
    r = Repo(SqliteBackend(":memory:"))
    r.init_schema()
    sample_data.seed_masters(r, confirmed_on=date(2026, 11, 1))
    sample_data.seed_month(r, MONTH)
    return r


def rule_ids(findings):
    return sorted({f.rule for f in findings})


# ------------------------------------------------------------ ルール判定

def test_must_rules_detect_each_violation(repo):
    ctx = repo.load(MONTH).ctx
    am, pm = slot_id_of(D1, "AM"), slot_id_of(D1, "PM")
    ok = Assignment("a1", am, "c_illust", "t_01", "r_404")
    assert rules.check_must([ok], ctx) == []

    # R1: 井上は 12/6 午前に対応可能日を出していない
    assert rule_ids(rules.check_must([Assignment("a", am, "c_illust", "t_02", "r_404")], ctx)) == ["R1"]
    # R2: 401 に CLIP STUDIO は入っていない
    assert rule_ids(rules.check_must([Assignment("a", am, "c_illust", "t_01", "r_401")], ctx)) == ["R2"]
    # R3: 同じ枠で同じ教室
    two = [ok, Assignment("a2", am, "c_anime", "t_03", "r_404")]
    assert "R3" in rule_ids(rules.check_must(two, ctx))
    # R4: 同じ枠で同じ講師
    two = [Assignment("a1", pm, "c_illust", "t_01", "r_404"), Assignment("a2", pm, "c_anime", "t_01", "r_403")]
    assert "R4" in rule_ids(rules.check_must(two, ctx))
    # R9: 12/13 午前の 404 は保護者会
    blocked = Assignment("a", slot_id_of(D2, "AM"), "c_illust", "t_01", "r_404")
    assert "R9" in rule_ids(rules.check_must([blocked], ctx))
    # 担当できない授業
    assert "担当" in rule_ids(rules.check_must([Assignment("a", am, "c_cg", "t_01", "r_402")], ctx))


def test_r2_uses_software_installed_on_that_date(repo):
    repo.replace("room_software", [
        {"room_id": "r_404", "software_id": "s_clip", "valid_from": date(2026, 12, 10), "valid_to": None,
         "confirmed_on": date(2026, 11, 1)}])
    ctx = repo.load(MONTH).ctx
    assert rules.missing_software(ctx, "r_404", "c_illust", D1) == ["s_clip"]  # 導入前
    assert rules.missing_software(ctx, "r_404", "c_illust", D3) == []  # 導入後


def test_r5_gap_and_substitute(repo):
    ctx = repo.load(MONTH).ctx
    cg = [Assignment("a", slot_id_of(D1, "AM"), "c_cg", "t_07", "r_402")]
    gaps = {(g, ctx.slots[slot_id_of(D2, "AM")].week_key) for g, w1, _ in
            rules.r5_gaps(rules.genre_coverage(cg, ctx), ctx) if g == "g_cg" for w1 in [w1]}
    assert gaps  # CG は 2 週目以降のどこかで 2 週続けて空く

    # VFX(CG の代わりにできる系統)が 3 週目に入れば、CG は「入っている」とみなす
    with_vfx = cg + [Assignment("b", slot_id_of(D3, "AM"), "c_vfx", "t_08", "r_402")]
    assert not [g for g, _, _ in rules.r5_gaps(rules.genre_coverage(with_vfx, ctx), ctx) if g == "g_cg"]


def test_r10_warns_on_stale_software_info(repo):
    repo.replace("room_software", [
        {"room_id": "r_404", "software_id": "s_clip", "valid_from": None, "valid_to": None,
         "confirmed_on": date(2025, 4, 1)}])
    ctx = repo.load(MONTH).ctx
    a = Assignment("a", slot_id_of(D1, "AM"), "c_illust", "t_01", "r_404")
    assert rule_ids(rules.check_warnings([a], ctx)) == ["R10"]


# ------------------------------------------------------------ 割り当て作成

def test_generated_plan_has_no_must_violation_and_places_everyone(repo):
    ctx = repo.load(MONTH).ctx
    plan = solver.generate(ctx)
    assert plan
    assert rules.check_must(plan, ctx) == []
    assert rules.unassigned_teachers(plan, ctx) == []  # R8
    assert not rules.r5_gaps(rules.genre_coverage(plan, ctx), ctx)  # R5(CG は VFX で代わり)
    per_slot = {}
    for a in plan:
        per_slot[a.slot_id] = per_slot.get(a.slot_id, 0) + 1
    assert max(per_slot.values()) <= ctx.settings.per_slot_max


def test_generate_is_deterministic(repo):
    ctx = repo.load(MONTH).ctx
    key = lambda plan: [(a.slot_id, a.course_id, a.teacher_id, a.room_id) for a in plan]
    assert key(solver.generate(ctx)) == key(solver.generate(ctx))


def test_cg_weeks_are_covered_by_substitute_with_reason(repo):
    ctx = repo.load(MONTH).ctx
    plan = solver.generate(ctx)
    reasons.fill_reasons(plan, ctx)
    late_weeks = {ctx.slots[slot_id_of(d, "AM")].week_key for d in (D3, D4)}
    vfx_late = [a for a in plan if ctx.genre_of(a.course_id) == "g_vfx"
                and ctx.slots[a.slot_id].week_key in late_weeks]
    assert vfx_late  # 木村(CG)が来られない後半は VFX 系で代わり
    assert any("F9" in a.reason for a in plan)
    assert all(a.reason for a in plan)


def test_explain_unplaced_names_the_rule(repo):
    # 加藤の唯一の対応可能枠で、Unity が使える教室を両方ふさぐ
    only = slot_id_of(D2, "PM")
    repo.save_room_blocks(MONTH, {("r_401", only): "esports大会", ("r_403", only): "esports大会"})
    ctx = repo.load(MONTH).ctx
    plan = solver.generate(ctx)
    assert "t_06" in rules.unassigned_teachers(plan, ctx)
    notes = [n for n in solver.explain_unplaced(plan, ctx) if "加藤" in n]
    assert notes and "R9" in notes[0]


def test_week_target_is_followed(repo):
    ctx = repo.load(MONTH).ctx
    week = ctx.slots[slot_id_of(D1, "AM")].week_key
    repo.save_week_targets(MONTH, {(week, "g_illust"): 2})
    ctx = repo.load(MONTH).ctx
    plan = solver.generate(ctx)
    assert rules.genre_coverage(plan, ctx).get((week, "g_illust"), 0) >= 2


# ------------------------------------------------------------ 手順(割り当て管理)

def test_full_flow_create_edit_confirm_messages(repo):
    assert manager.create_plan(repo, MONTH) > 0
    bundle = repo.load(MONTH)
    assert bundle.status == "draft"

    # 先生が 1 行を、ソフトの入っていない教室に直す → 再チェックで R2、確定できない
    rows = [vars(a).copy() for a in bundle.assignments]
    target = next(r for r in rows if r["course_id"] == "c_illust")
    target["room_id"] = "r_402"
    assert manager.save_edits(repo, MONTH, rows, "テスト") == 1
    assert "R2" in rule_ids(manager.confirm(repo, MONTH))
    assert repo.load(MONTH).status == "draft"
    assert repo.count_changes(MONTH) == 1

    # 元に戻して確定
    target["room_id"] = "r_404"
    manager.save_edits(repo, MONTH, rows, "")
    assert manager.confirm(repo, MONTH) == []
    bundle = repo.load(MONTH)
    assert bundle.status == "confirmed"
    assert all(a.status == "confirmed" for a in bundle.assignments)

    # 連絡文: 割り当てとの食い違いは 0 件
    assert manager.create_messages(repo, MONTH, "鈴木") == []
    bundle = repo.load(MONTH)
    kinds = {m.kind for m in bundle.messages}
    assert {"notice", "hp"} <= kinds
    aoki = next(m for m in bundle.messages if m.teacher_id == "t_01")
    assert aoki.body.startswith("青木先生")
    for line in messages.teacher_schedule("t_01", bundle.assignments, bundle.ctx):
        assert line in aoki.body


def test_messages_cannot_be_created_before_confirm(repo):
    manager.create_plan(repo, MONTH)
    with pytest.raises(ValueError):
        manager.create_messages(repo, MONTH)


def test_unplaced_teacher_gets_thanks_message(repo):
    only = slot_id_of(D2, "PM")
    repo.save_room_blocks(MONTH, {("r_401", only): "esports大会", ("r_403", only): "esports大会"})
    manager.create_plan(repo, MONTH)
    assert manager.confirm(repo, MONTH) == []
    manager.create_messages(repo, MONTH)
    kato = next(m for m in repo.load(MONTH).messages if m.teacher_id == "t_06")
    assert kato.kind == "thanks" and "来月" in kato.body


def test_previous_month_confirmed_assignments_feed_r5(repo):
    manager.create_plan(repo, MONTH)
    manager.confirm(repo, MONTH)
    sample_data.seed_month(repo, "2027-01")
    ctx = repo.load("2027-01").ctx
    assert ctx.previous and all(p.day.month == 12 for p in ctx.previous)


# ------------------------------------------------------------ 取り込み

def test_import_availability_csv(repo):
    text = "講師名,日付,時間帯\n青木先生,2026/12/6,午前\n井上,12/13(日),終日\n山田,2026-12-06,午前\n青木,2026-12-07,午前\n"
    n, notes = manager.import_availability(repo, MONTH, manager.parse_availability_csv(text))
    ctx = repo.load(MONTH).ctx
    assert n == 3
    assert ctx.availability == {
        ("t_01", slot_id_of(D1, "AM")), ("t_02", slot_id_of(D2, "AM")), ("t_02", slot_id_of(D2, "PM"))}
    assert any("山田" in x for x in notes) and any("12/7" in x for x in notes)


def test_import_from_form_raw_table(repo):
    repo.insert("availability_raw", [
        {"received_at": "2026-10-20T10:00:00", "teacher_name": "木村", "day": D4, "period": "午後"}])
    n, _ = manager.import_availability_from_form(repo, MONTH)
    assert n == 1 and repo.load(MONTH).ctx.availability == {("t_07", slot_id_of(D4, "PM"))}


def test_import_past_assignments_and_check(repo):
    text = "日付,時間帯,体験授業,講師,教室\n2026-12-06,午前,デジタルイラスト体験,青木,404\n" \
           "2026-12-06,午前,3DCGモデリング体験,木村,401\n"
    n, notes = manager.import_assignments_csv(repo, MONTH, text)
    assert n == 2 and notes == []
    bundle = repo.load(MONTH)
    assert rule_ids(rules.check_must(bundle.assignments, bundle.ctx)) == ["R2"]  # 401 に Maya はない


# ------------------------------------------------------------ LLM(講師名を渡さない)

def test_llm_never_sees_teacher_names(repo):
    seen = []

    def transport(system, user):
        seen.append(system + user)
        return user  # そのまま返す

    client = llm.LLMClient(transport=transport)
    out = client.complete("説明して", "青木先生と工藤先生の割り当て", ["青木", "工藤"])
    assert "青木" not in seen[0] and "工藤" not in seen[0] and "講師A" in seen[0]
    assert out == "青木先生と工藤先生の割り当て"


def test_polish_reasons_and_messages_with_llm(repo):
    prompts = []

    def transport(system, user):
        prompts.append(user)
        if "JSON" in system:
            import json
            return json.dumps({item["id"]: f"{item['講師']}の都合に合わせました。" for item in json.loads(user)},
                              ensure_ascii=False)
        return user.replace("お疲れさまです。", "いつもありがとうございます。")

    client = llm.LLMClient(transport=transport)
    manager.create_plan(repo, MONTH)
    assert manager.polish_reasons(repo, MONTH, client)
    bundle = repo.load(MONTH)
    names = [t.name for t in bundle.ctx.teachers.values()]
    assert all(not any(n in p for n in names) for p in prompts)
    first = bundle.assignments[0]
    assert first.reason == f"{bundle.ctx.teacher_name(first.teacher_id)}の都合に合わせました。"

    manager.confirm(repo, MONTH)
    assert manager.create_messages(repo, MONTH, "鈴木", client) == []
    assert any("いつもありがとうございます。" in m.body for m in repo.load(MONTH).messages)
    assert all(not any(n in p for n in names) for p in prompts)


def test_llm_output_that_drops_schedule_placeholder_is_rejected(repo):
    client = llm.LLMClient(transport=lambda s, u: "日程は後ほどお送りします。")
    manager.create_plan(repo, MONTH)
    manager.confirm(repo, MONTH)
    assert manager.create_messages(repo, MONTH, "鈴木", client) == []  # 定型文に戻るので食い違いなし


def test_settings_roundtrip(repo):
    s = repo.load_settings()
    s.per_slot_max, s.w_r8 = 4, 99
    repo.save_settings(s)
    loaded = repo.load_settings()
    assert (loaded.per_slot_max, loaded.w_r8) == (4, 99)


def test_llm_uses_ai_gateway_for_uc_model_service(monkeypatch):
    import requests

    calls = []

    class FakeConfig:
        host = "https://example.cloud.databricks.com"

        def authenticate(self):
            return {"Authorization": "Bearer x"}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "講師Aです"}}]}

    monkeypatch.setattr("databricks.sdk.core.Config", FakeConfig)
    monkeypatch.setattr(requests, "post", lambda url, **kw: calls.append((url, kw["json"])) or FakeResp())

    assert llm.LLMClient("system.ai.gemma-3-12b").complete("s", "青木", ["青木"]) == "青木です"
    assert calls[-1][0].endswith("/ai-gateway/mlflow/v1/chat/completions")
    assert calls[-1][1]["model"] == "system.ai.gemma-3-12b"

    llm.LLMClient("databricks-gpt-oss-20b").complete("s", "u", [])
    assert calls[-1][0].endswith("/serving-endpoints/databricks-gpt-oss-20b/invocations")
    assert "model" not in calls[-1][1]
