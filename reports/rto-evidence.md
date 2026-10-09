# RTO/RPO Evidence — Day 23

Đo tại 2026-10-09 UTC bằng bare mode, `netblock --mock`, traffic qua edge 2 request/s. Mọi mốc dùng trường `ts` (giây Unix) của log; `tools/measure_rto.py` là phép tính đối chiếu. Đây là hai drill chạy từ state sạch.

## Drill 1 — không có DR

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---:|---|---|
| t_outage | 2026-10-09T12:39:43.387Z | `action:kill`, Region A | `chaos/chaos-events.jsonl:1` |
| Request lỗi đầu tiên sau kill | +0.5s | HTTP 503, `ReadTimeout` | `reports/drill-1-nodr.jsonl:18` |
| Request cuối cửa sổ vẫn lỗi | +30.7s | Không có thành công sau kill; 16 request lỗi | `reports/drill-1-nodr.jsonl:33` |
| RTO | NO_RECOVERY | `tools/measure_rto.py` không tìm thấy request phục hồi | `reports/drill-1-nodr.jsonl:33` |

## Drill 2 — có DR

| Mốc | Thời điểm UTC | +giây từ t_outage | Evidence |
|---|---|---:|---|
| Outage Region A | 12:40:56.677 | 0.00s | `chaos/chaos-events.jsonl:3` |
| Request lỗi đầu tiên bắt đầu sau kill | 12:40:58.694 | 2.02s | `reports/drill-2-withdr.jsonl:26` |
| Health checker chuyển A sang UNHEALTHY | 12:41:11.648 | 14.97s | `reports/health-events.jsonl:2` |
| Operator ghi nhận incident | 12:41:11.923 | 15.25s | `reports/runbook-run.jsonl:2` |
| Snapshot restore xong | 12:41:11.942 | 15.27s | `reports/failover-events.jsonl:2` |
| B ready sau GPU warm-up | 12:41:18.094 | 21.42s | `reports/failover-events.jsonl:4` |
| DNS/LB cutover | 12:41:18.107 | 21.43s | `reports/failover-events.jsonl:5` |
| Request đầu tiên thành công từ B | 12:41:18.841 | 22.16s (RTO làm tròn 22.2s) | `reports/drill-2-withdr.jsonl:36` |

| Chỉ số | Đo được | Mục tiêu | Verdict | Evidence |
|---|---:|---:|---|---|
| RTO — inference API | 22.2s | 300s | PASS, ít hơn mục tiêu 277.8s | `chaos/chaos-events.jsonl:3`, `reports/drill-2-withdr.jsonl:36` |
| RPO — vector DB | 6.0s / 3 document | 300s | PASS, ít hơn mục tiêu 294.0s | `reports/failover-events.jsonl:2` |
| Model embedding version | `embed-model=vi-e5-base@v3` | Khớp snapshot | PASS | `reports/failover-events.jsonl:2` |
| Golden signals tại B | 10 request, 0% lỗi, p95 19.8ms | Không có lỗi | PASS | `reports/runbook-run.jsonl:6` |

Health check cấu hình `interval_s=5`, `threshold=3`, detect floor = 5 × 3 = 15s; quan sát thực tế 14.97s do pha lấy mẫu và làm tròn. Evidence: `reports/health-events.jsonl:2`.

## Phân rã RTO (tổng 22.16s ≈ RTO làm tròn 22.2s)

| Thành phần | Giây | Ranh giới timestamp và evidence | Cách giảm |
|---|---:|---|---|
| Health-check detection | 14.97s | kill → A UNHEALTHY: `chaos/chaos-events.jsonl:3`, `reports/health-events.jsonl:2` | Giảm interval sau khi kiểm chứng tỷ lệ false alert. |
| Snapshot restore và xác minh | 0.29s | A UNHEALTHY → B scale; gồm notify, verify target và copy snapshot: `reports/health-events.jsonl:2`, `reports/failover-events.jsonl:1`, `reports/failover-events.jsonl:2`, `reports/failover-events.jsonl:3` | Giữ snapshot mới, giảm kích thước bản sao. |
| GPU pool warm-up | 6.15s | B scale → B ready, `waited_s=6.15`: `reports/failover-events.jsonl:3`, `reports/failover-events.jsonl:4` | Duy trì pool B sẵn sàng nếu có ngân sách. |
| DNS/LB TTL và request kế tiếp | 0.75s | B ready → request thành công; cutover nằm giữa: `reports/failover-events.jsonl:4`, `reports/failover-events.jsonl:5`, `reports/drill-2-withdr.jsonl:36` | Giảm TTL nếu cache là nút thắt ở nhiều lần drill. |

Khoảng 0.29s của pha restore bao gồm overhead điều phối; thời gian copy thuần từ `1_verify_target` đến `2_restore_snapshot` là khoảng 0.005s. TTL cấu hình 5s nhưng lần này cache đổi sau khoảng 0.73s từ cutover. RPO lấy từ so sánh timestamp document của primary và bản restore, cùng số document vắng mặt, không lấy tuổi của snapshot.
