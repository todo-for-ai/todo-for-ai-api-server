#!/usr/bin/env python3
"""GoalLoop 目标循环 E2E 验收脚本

前提：
- 后端已启动并以 GOAL_LOOP_PLANNER=scripted、GOAL_LOOP_SCRIPTED_ROUNDS=3 运行
- 迁移 000017 已应用

验证链路（全部走真实 HTTP）：
1. 创建循环 → 第 1 轮任务自动生成并 auto-assign（租约即建）
2. 模拟 agent 完成（PUT /tasks/{id} status=done，触发真实路由挂钩）→ 第 2 轮生成
3. 循环至 scripted 规划器宣告完成 → loop=done + completion_summary
4. 护栏：rounds_limit=2 + scripted 99 轮 → limit_reached，且终态不再推进
"""

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:50110/todo-for-ai/api/v1"
PROJECT_ID = 1083  # org 5 有 28 个活跃 agent
JWT = open("/tmp/t4ai_jwt.txt").read().strip()


def call(method, path, body=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {JWT}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def wait_for(predicate, timeout=15, what=""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.5)
    print(f"❌ 等待超时: {what}")
    return False


def get_loop(loop_id):
    status, body = call("GET", f"/projects/goal-loops/{loop_id}")
    assert status == 200, body
    return body["data"]


def finish_last_task(loop):
    task = loop["tasks"][-1]
    status, body = call("PUT", f"/tasks/{task['id']}", {"status": "done"})
    assert status == 200, body


def main():
    ok = True

    # ── 场景 1：三轮完成 ──
    status, body = call("POST", f"/projects/{PROJECT_ID}/goal-loops", {
        "title": "E2E 目标循环",
        "goal_text": "E2E 验证：连续完成三轮任务",
        "done_definition": "scripted 三轮完成",
        "rounds_limit": 5,
    })
    assert status == 200, body
    loop = body["data"]
    loop_id = loop["id"]
    print(f"✅ 循环创建 loop={loop_id}，第 1 轮任务 task={loop['tasks'][0]['id']} "
          f"status={loop['tasks'][0]['status']} rounds={loop['rounds_done']}")
    assert loop["rounds_done"] == 1
    assert loop["tasks"][0]["status"] == "in_progress", "应已被 auto-assign 置为进行中"
    # v2：创建即拆解出计划（scripted 3 步）
    assert len(loop["plan"]) == 3, f"应有 3 步计划，实际 {len(loop['plan'])}"
    assert loop["plan_index"] == 1, "第 1 步应已物化"
    print(f"✅ 计划式拆解：{len(loop['plan'])} 步计划，plan_index={loop['plan_index']}")

    for round_no in range(3):
        loop = get_loop(loop_id)
        finish_last_task(loop)
        if not wait_for(lambda: get_loop(loop_id)["rounds_done"] > loop["rounds_done"] or get_loop(loop_id)["status"] == "done"):
            ok = False
            break
        loop = get_loop(loop_id)
        print(f"✅ 第 {round_no + 1} 轮完成 → rounds={loop['rounds_done']} status={loop['status']}")

    loop = get_loop(loop_id)
    if loop["status"] == "done" and "目标达成" in (loop["completion_summary"] or ""):
        print(f"✅ 循环宣告完成: {loop['completion_summary']}")
    else:
        print(f"❌ 循环未正确收尾: status={loop['status']} summary={loop.get('completion_summary')}")
        ok = False

    # ── 场景 2：轮数上限护栏 ──
    status, body = call("POST", f"/projects/{PROJECT_ID}/goal-loops", {
        "title": "E2E 护栏",
        "goal_text": "E2E 验证：轮数上限（scripted 需要 99 轮才宣告完成）",
        "rounds_limit": 2,
    })
    assert status == 200, body
    loop = body["data"]
    guard_id = loop["id"]

    for i in range(2):
        loop = get_loop(guard_id)
        finish_last_task(loop)
        wait_for(lambda: get_loop(guard_id)["status"] != "running" or get_loop(guard_id)["rounds_done"] > loop["rounds_done"])
        loop = get_loop(guard_id)

    if loop["status"] == "limit_reached" and "轮数上限" in (loop["last_error"] or ""):
        print(f"✅ 轮数上限护栏生效: status={loop['status']} error={loop['last_error']}")
    else:
        print(f"❌ 护栏未生效: status={loop['status']} last_error={loop.get('last_error')}")
        ok = False

    before = loop["rounds_done"]
    finish_last_task(loop)
    loop = get_loop(guard_id)
    if loop["rounds_done"] == before and loop["status"] == "limit_reached":
        print("✅ 终态后不再推进")
    else:
        print("❌ 终态后仍推进")
        ok = False

    # ── 场景 3：多 Agent 编排（指挥者 + 步骤岗位返回） ──
    status, body = call("GET", f"/projects/{PROJECT_ID}/overview")
    agents = (body.get("data") or {}).get("agents") or []
    director = next((a for a in agents if a.get("role")), None)
    status, body = call("POST", f"/projects/{PROJECT_ID}/goal-loops", {
        "title": "E2E 编排",
        "goal_text": "E2E 验证：指挥者拆解评审（scripted 规划器退化为单 Agent）",
        "director_agent_id": (director or {}).get("id"),
    })
    assert status == 200, body
    loop = body["data"]
    orch_id = loop["id"]
    if (director and loop.get("director_agent_id") == director["id"]
            and (loop.get("director_display_name") or loop.get("director_name"))):
        print(f"✅ 指挥者已绑定: director={loop.get('director_display_name') or loop.get('director_name')}")
    elif director is None:
        print("⏭️  项目无绑定角色的 Agent，跳过指挥者断言")
    else:
        print(f"❌ 指挥者未正确绑定: {loop.get('director_agent_id')} != {director['id']}")
        ok = False
    t0 = loop["tasks"][0]
    if "agent_id" in t0:
        print(f"✅ 轮次任务带执行者: task={t0['id']} agent={t0.get('agent_name') or t0.get('agent_id')}")
    else:
        print("❌ 轮次任务缺少执行者字段")
        ok = False
    status, _ = call("POST", f"/projects/goal-loops/{orch_id}/stop")
    assert status == 200

    # ── 场景 4：人工停止 ──
    status, body = call("POST", f"/projects/{PROJECT_ID}/goal-loops", {
        "title": "E2E 停止", "goal_text": "验证人工停止",
    })
    loop = body["data"]
    status, _ = call("POST", f"/projects/goal-loops/{loop['id']}/stop")
    assert status == 200
    loop = get_loop(loop["id"])
    if loop["status"] == "stopped":
        print("✅ 人工停止生效")
    else:
        print(f"❌ 停止失败: {loop['status']}")
        ok = False

    print("\n" + ("🎉 E2E 全部通过" if ok else "💥 E2E 存在失败"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
