"""审计快照端到端测试：通过 HTTP 打在运行中的服务上。

覆盖：
- 左闭右开窗口边界；只取最新版本 measured_at（更正移出/移入窗口）
- 单条与批次写入均纳入；空窗计数为零
- 明细固定字段；按测量编号游标稳定分页
- 冻结性：快照生成后的撤销/更正/新写入不改变既有明细与总数
- 一致性：快照生成与撤销/更正并发时，版本与撤销状态取自同一视图（整体在前或在后）
- 校验：缺时区/起点不早于终点 422 且不留快照；未知快照 404
"""

import concurrent.futures
import os
import threading
import uuid

import httpx
import pytest

BASE = os.environ.get("BASE", "http://127.0.0.1:8000")
API = f"{BASE}/api/v1"

RUN = uuid.uuid4().hex[:8]


def _device(n: int) -> str:
    return f"TS-{RUN}-{n:03d}"


@pytest.fixture(scope="session")
def client():
    with httpx.Client(base_url=API, timeout=30) as c:
        yield c


_idem = 0


def _idem_key() -> str:
    global _idem
    _idem += 1
    return f"ts-{RUN}-{_idem:05d}"


def register_cert(client, device, no, vfrom="2026-01-01T00:00:00+08:00",
                  vto="2027-01-01T00:00:00+08:00"):
    r = client.post("/certificates",
                    json={"device_id": device, "certificate_no": no,
                          "valid_from": vfrom, "valid_to": vto},
                    headers={"Idempotency-Key": _idem_key()})
    assert r.status_code in (200, 201), r.text
    return r.json()


def write_measurement(client, device, at, no):
    r = client.post("/measurements",
                    json={"device_id": device, "measured_at": at, "certificate_no": no})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def make_snapshot(client, device, start, end, expected=201):
    r = client.post("/audit-snapshots",
                    json={"device_id": device, "window_start": start, "window_end": end})
    assert r.status_code == expected, r.text
    return r.json()


def get_snapshot(client, sid):
    r = client.get(f"/audit-snapshots/{sid}")
    assert r.status_code == 200, r.text
    return r.json()


def fetch_all_items(client, sid, limit=2):
    """按游标翻完全部页，返回 (items, cursors_seen)。"""
    items, after = [], None
    while True:
        params = {"limit": limit}
        if after is not None:
            params["after_measurement_id"] = after
        r = client.get(f"/audit-snapshots/{sid}/items", params=params)
        assert r.status_code == 200, r.text
        page = r.json()
        items.extend(page["items"])
        if page["next_after_measurement_id"] is None:
            break
        after = page["next_after_measurement_id"]
    return items


def test_window_boundaries_and_latest_version_only(client):
    device = _device(1)
    register_cert(client, device, "C-T1")
    # 三条：窗口起点处（含）、窗口内、窗口终点处（不含）
    id_at_start = write_measurement(client, device, "2026-06-01T00:00:00+08:00", "C-T1")
    id_mid = write_measurement(client, device, "2026-06-15T10:30:00+08:00", "C-T1")
    id_at_end = write_measurement(client, device, "2026-07-01T00:00:00+08:00", "C-T1")

    snap = make_snapshot(client, device,
                         "2026-06-01T00:00:00+08:00", "2026-07-01T00:00:00+08:00")
    assert snap["valid_count"] == 2 and snap["invalid_count"] == 0
    assert snap["total_count"] == 2
    ids = {it["measurement_id"] for it in fetch_all_items(client, snap["snapshot_id"])}
    assert ids == {id_at_start, id_mid}
    assert id_at_end not in ids


def test_correction_moves_record_out_of_then_back_into_window(client):
    device = _device(2)
    register_cert(client, device, "C-T2A",
                  "2026-01-01T00:00:00+08:00", "2027-01-01T00:00:00+08:00")
    register_cert(client, device, "C-T2B",
                  "2027-01-01T00:00:00+08:00", "2028-01-01T00:00:00+08:00")
    mid = write_measurement(client, device, "2026-06-15T10:30:00+08:00", "C-T2A")

    # 首次快照：记录在窗口内
    snap1 = make_snapshot(client, device,
                          "2026-01-01T00:00:00+08:00", "2027-01-01T00:00:00+08:00")
    assert snap1["total_count"] == 1

    # 更正把时刻移出窗口（换 2027 证书）
    r = client.post(f"/measurements/{mid}/corrections",
                    json={"base_version": 1,
                          "measured_at": "2027-03-10T09:00:00+08:00",
                          "certificate_no": "C-T2B", "reason": "move out"})
    assert r.status_code == 201, r.text

    snap2 = make_snapshot(client, device,
                          "2026-01-01T00:00:00+08:00", "2027-01-01T00:00:00+08:00")
    assert snap2["total_count"] == 0  # 只看最新版本时刻 → 空窗
    assert fetch_all_items(client, snap2["snapshot_id"]) == []

    # 而旧快照 snap1 仍保留旧版本（version=1）明细，不受更正影响
    old_items = fetch_all_items(client, snap1["snapshot_id"])
    assert len(old_items) == 1
    assert old_items[0]["measurement_id"] == mid and old_items[0]["version"] == 1

    # 新窗口覆盖更正后时刻 → 该测量以 version=2 命中
    snap3 = make_snapshot(client, device,
                          "2027-01-01T00:00:00+08:00", "2028-01-01T00:00:00+08:00")
    items3 = fetch_all_items(client, snap3["snapshot_id"])
    assert [(i["measurement_id"], i["version"]) for i in items3] == [(mid, 2)]
    assert items3[0]["certificate_no"] == "C-T2B"


def test_batch_writes_included(client):
    device = _device(3)
    cert = register_cert(client, device, "C-T3")
    r = client.post("/measurement-batches",
                    json={"batch_key": f"ts-batch-{RUN}",
                          "records": [
                              {"device_id": device,
                               "measured_at": "2026-06-10T08:00:00+08:00",
                               "certificate_no": "C-T3"},
                              {"device_id": device,
                               "measured_at": "2026-06-11T08:00:00+08:00",
                               "certificate_no": "C-T3"}]})
    assert r.status_code == 201, r.text
    snap = make_snapshot(client, device,
                         "2026-01-01T00:00:00+08:00", "2027-01-01T00:00:00+08:00")
    assert snap["total_count"] == 2
    # 明细字段固定为 measurement_id/version/measured_at/certificate_no/validity
    items = fetch_all_items(client, snap["snapshot_id"])
    assert set(items[0].keys()) == {
        "measurement_id", "version", "measured_at", "certificate_no", "validity"}
    assert set(items[0]["validity"].keys()) == {"is_valid", "reason"}
    assert all(it["version"] == 1 and it["validity"]["is_valid"] for it in items)
    assert cert  # noqa


def test_empty_window_counts_zero_but_snapshot_exists(client):
    device = _device(4)
    register_cert(client, device, "C-T4")
    snap = make_snapshot(client, device,
                         "2030-01-01T00:00:00+08:00", "2031-01-01T00:00:00+08:00")
    assert snap["valid_count"] == snap["invalid_count"] == snap["total_count"] == 0
    assert get_snapshot(client, snap["snapshot_id"])["total_count"] == 0
    r = client.get(f"/audit-snapshots/{snap['snapshot_id']}/items")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == [] and body["next_after_measurement_id"] is None


def test_invalid_records_counted_and_frozen_after_revoke(client):
    device = _device(5)
    cert = register_cert(client, device, "C-T5")
    cert_id = cert["id"]
    keep, drop = (
        write_measurement(client, device, "2026-06-01T10:00:00+08:00", "C-T5"),
        write_measurement(client, device, "2026-12-31T10:00:00+08:00", "C-T5"),
    )

    # 撤销前生成快照：2 条全部有效
    before = make_snapshot(client, device,
                           "2026-01-01T00:00:00+08:00", "2027-01-01T00:00:00+08:00")
    assert before["valid_count"] == 2 and before["invalid_count"] == 0

    # 撤销证书
    r = client.post(f"/certificates/{cert_id}/revoke", json={"reason": "drift found"})
    assert r.status_code == 200, r.text

    # 新快照：2 条全部无效，原因冻结撤销时刻/原因
    after = make_snapshot(client, device,
                          "2026-01-01T00:00:00+08:00", "2027-01-01T00:00:00+08:00")
    assert after["valid_count"] == 0 and after["invalid_count"] == 2
    for it in fetch_all_items(client, after["snapshot_id"]):
        assert it["validity"]["is_valid"] is False
        assert "certificate revoked at" in it["validity"]["reason"]
        assert "drift found" in it["validity"]["reason"]

    # 旧快照仍为 2 有效（撤销不改变已生成的明细和总数）
    assert get_snapshot(client, before["snapshot_id"])["valid_count"] == 2
    old_items = fetch_all_items(client, before["snapshot_id"])
    assert all(it["validity"]["is_valid"] for it in old_items)
    assert {it["measurement_id"] for it in old_items} == {keep, drop}


def test_new_writes_and_corrections_do_not_change_old_snapshot(client):
    device = _device(6)
    register_cert(client, device, "C-T6")
    first = write_measurement(client, device, "2026-06-01T10:00:00+08:00", "C-T6")
    snap = make_snapshot(client, device,
                         "2026-01-01T00:00:00+08:00", "2027-01-01T00:00:00+08:00")
    assert snap["total_count"] == 1

    write_measurement(client, device, "2026-06-02T10:00:00+08:00", "C-T6")
    r = client.post(f"/measurements/{first}/corrections",
                    json={"base_version": 1, "measured_at": "2026-06-03T10:00:00+08:00",
                          "reason": "later correction"})
    assert r.status_code == 201, r.text

    # 头与明细都不变
    frozen = get_snapshot(client, snap["snapshot_id"])
    assert frozen["total_count"] == 1 and frozen["valid_count"] == 1
    items = fetch_all_items(client, snap["snapshot_id"])
    assert len(items) == 1 and items[0]["version"] == 1
    assert items[0]["measurement_id"] == first


def test_pagination_stable_and_ordered(client):
    device = _device(7)
    register_cert(client, device, "C-T7")
    ids = [write_measurement(client, device, f"2026-05-{d:02d}T08:00:00+08:00", "C-T7")
           for d in range(1, 8)]  # 7 条

    snap = make_snapshot(client, device,
                         "2026-01-01T00:00:00+08:00", "2027-01-01T00:00:00+08:00")
    sid = snap["snapshot_id"]

    # limit=3 逐页：顺序严格按 measurement_id 升序，游标稳定
    pages, after = [], None
    while True:
        params = {"limit": 3}
        if after is not None:
            params["after_measurement_id"] = after
        body = client.get(f"/audit-snapshots/{sid}/items", params=params).json()
        pages.append(body)
        if body["next_after_measurement_id"] is None:
            break
        after = body["next_after_measurement_id"]
    flat = [it["measurement_id"] for p in pages for it in p["items"]]
    assert flat == sorted(ids)
    assert [len(p["items"]) for p in pages] == [3, 3, 1]
    # 末页无下一页游标
    assert pages[-1]["next_after_measurement_id"] is None

    # 从任意游标开始结果稳定：取第 2 页游标后直接请求
    cur = pages[0]["next_after_measurement_id"]
    body = client.get(f"/audit-snapshots/{sid}/items",
                      params={"limit": 3, "after_measurement_id": cur}).json()
    assert [it["measurement_id"] for it in body["items"]] == flat[3:6]


def test_reject_naive_datetime_and_bad_window_no_snapshot_left(client):
    device = _device(8)
    # 缺时区 → 422
    r1 = client.post("/audit-snapshots",
                     json={"device_id": device,
                           "window_start": "2026-01-01T00:00:00",
                           "window_end": "2026-07-01T00:00:00+08:00"})
    assert r1.status_code == 422
    r2 = client.post("/audit-snapshots",
                     json={"device_id": device,
                           "window_start": "2026-07-01T00:00:00+08:00",
                           "window_end": "2026-01-01T00:00:00+08:00"})
    assert r2.status_code == 422
    # 起点等于终点（不是"早于"）→ 422
    r3 = client.post("/audit-snapshots",
                     json={"device_id": device,
                           "window_start": "2026-01-01T00:00:00+08:00",
                           "window_end": "2026-01-01T00:00:00+08:00"})
    assert r3.status_code == 422

    # 不留快照：该设备没有任何快照
    rows = client.get("/audit-snapshots/99999999")
    assert rows.status_code == 404
    # 直接查库确认该设备无快照行
    import asyncpg, asyncio
    async def _check():
        conn = await asyncpg.connect(
            "postgresql://postgres@localhost/calibration?host=/tmp/pgrun")
        n = await conn.fetchval(
            "SELECT count(*) FROM audit_snapshots WHERE device_id=$1", device)
        await conn.close()
        return n
    assert asyncio.run(_check()) == 0


def test_unknown_snapshot_404(client):
    r = client.get("/audit-snapshots/424242")
    assert r.status_code == 404 and r.json()["error"]["code"] == "SNAPSHOT_NOT_FOUND"
    r = client.get("/audit-snapshots/424242/items")
    assert r.status_code == 404 and r.json()["error"]["code"] == "SNAPSHOT_NOT_FOUND"


def test_timezone_equivalence_same_window(client):
    device = _device(9)
    register_cert(client, device, "C-T9")
    mid = write_measurement(client, device, "2026-06-15T10:30:00+08:00", "C-T9")
    # 同一绝对窗口的不同时区写法等价：02:30Z == 10:30+08:00
    snap = make_snapshot(client, device,
                         "2026-06-15T02:30:00+00:00", "2026-06-15T02:31:00+00:00")
    items = fetch_all_items(client, snap["snapshot_id"])
    assert [it["measurement_id"] for it in items] == [mid]


def test_snapshot_consistent_with_concurrent_revoke():
    """生成快照与撤销并发：每条快照结果必须对应"撤销前整体"或"撤销后整体"的一致视图。"""
    with httpx.Client(base_url=API, timeout=30) as c:
        results = []

        def trial(i):
            device = _device(100 + i)
            cert = register_cert(c, device, f"C-TC{i}")
            write_measurement(c, device, "2026-06-15T10:00:00+08:00", f"C-TC{i}")
            barrier = threading.Barrier(2)

            def snapshot_side():
                barrier.wait()
                return make_snapshot(
                    c, device,
                    "2026-01-01T00:00:00+08:00", "2027-01-01T00:00:00+08:00")

            def revoke_side():
                barrier.wait()
                r = c.post(f"/certificates/{cert['id']}/revoke",
                           json={"reason": f"concurrent revoke {i}"})
                return r.status_code

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
                fs = [ex.submit(snapshot_side), ex.submit(revoke_side)]
                snap = fs[0].result()
                rcode = fs[1].result()
            assert rcode == 200
            return device, snap

        for i in range(12):
            results.append(trial(i))

        # 撤销最终都已提交（实时视图全部无效）。每个快照必须是：
        # 整体在撤销前 -> valid=1；或整体在撤销后 -> invalid=1，reason 带该次撤销原因。
        with httpx.Client(base_url=API, timeout=30) as c2:
            for i, (device, snap) in enumerate(results):
                assert (snap["valid_count"], snap["invalid_count"]) in {(1, 0), (0, 1)}, snap
                items = fetch_all_items(c2, snap["snapshot_id"])
                assert len(items) == 1
                it = items[0]
                if snap["invalid_count"] == 1:
                    assert it["validity"]["is_valid"] is False
                    assert f"concurrent revoke {i}" in it["validity"]["reason"]
                else:
                    assert it["validity"]["is_valid"] is True
                # 再次读取：冻结不变
                again = get_snapshot(c2, snap["snapshot_id"])
                assert again["valid_count"] == snap["valid_count"]
                assert again["invalid_count"] == snap["invalid_count"]


def test_snapshot_consistent_with_concurrent_correction():
    """生成快照与更正并发：命中的版本要么全是 v1 要么全是 v2（版本与时刻同视图）。"""
    with httpx.Client(base_url=API, timeout=30) as c:
        for i in range(12):
            device = _device(200 + i)
            register_cert(c, device, f"C-TCA{i}",
                          "2026-01-01T00:00:00+08:00", "2028-01-01T00:00:00+08:00")
            mid = write_measurement(c, device, "2026-06-15T10:00:00+08:00", f"C-TCA{i}")
            barrier = threading.Barrier(2)

            def snapshot_side():
                barrier.wait()
                # 窗口同时覆盖 v1 与 v2 时刻，差异只体现在版本号/时刻上
                return make_snapshot(
                    c, device,
                    "2026-01-01T00:00:00+08:00", "2028-01-01T00:00:00+08:00")

            def correction_side():
                barrier.wait()
                r = c.post(f"/measurements/{mid}/corrections",
                           json={"base_version": 1,
                                 "measured_at": "2027-06-15T10:00:00+08:00",
                                 "reason": f"concurrent correction {i}"})
                return r.status_code

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
                fs = [ex.submit(snapshot_side), ex.submit(correction_side)]
                snap = fs[0].result()
                ccode = fs[1].result()
            assert ccode == 201
            assert snap["total_count"] == 1
            items = fetch_all_items(c, snap["snapshot_id"])
            (it,) = items
            assert it["measurement_id"] == mid
            assert it["version"] in (1, 2)  # 整体在前或在后，不会撕裂
            if it["version"] == 1:
                assert it["measured_at"].startswith("2026-06-15")
            else:
                assert it["measured_at"].startswith("2027-06-15")
