# Runbook — Region A down

Chạy tại thư mục gốc repo, với stack bare mode đang hoạt động. Mặc định cần người trực xác nhận `y`; chỉ dùng `--auto` khi chạy drill/CI. Không cutover nếu Region B chưa `/readyz` 200. Ghi thời điểm và kết quả vào `reports/runbook-run.jsonl`.

| # | Bước | Lệnh copy-paste | Tín hiệu hoàn thành | Owner |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` và `python3 dr/health_checker.py --interval 5 --threshold 3 --duration 30 --out reports/health-events.jsonl` | Region A `UNHEALTHY` sau ≥3 lần `/readyz` fail liên tiếp; Region B `/healthz` còn sống. | On-call SRE |
| 2 | Mở incident và bấm giờ | `python3 dr/runbook.py --primary a --target b --backend fs` | Xác nhận `y`; `thong_bao_incident` trong `reports/runbook-run.jsonl` có `outage_ts` và `operator_notified_ts`. Lệnh này tự chạy các bước 3–6 đúng một lần. | Incident commander |
| 3 | Restore vector DB và model weights | `python3 state/snapshot.py lag --backend fs` | `2_restore_snapshot` trong `reports/failover-events.jsonl` có `rpo_seconds`, `docs_lost`, `embed_model_version`; `state/region-b/weights/model.bin` tồn tại. | On-call SRE |
| 4 | Scale pool và chờ warm-up | `curl -i http://127.0.0.1:8002/readyz` | HTTP 200 và `ready:true`; `4_wait_ready` có `waited_s`. Nếu timeout, runbook abort và chưa cutover. | Serving operator |
| 5 | Xác nhận DNS/LB cutover | `curl http://127.0.0.1:8080/edge/state` | `active_region:b` sau khi cache TTL hết; `5_dns_cutover` trong `reports/failover-events.jsonl` đến sau `4_wait_ready`. | On-call SRE |
| 6 | Kiểm tra golden signals | `curl -i http://127.0.0.1:8080/v1/infer` | HTTP 200, `edge_region:b`, `region:b`; `verify_golden_signals` trong `reports/runbook-run.jsonl` ghi 10 request thật, `error_rate:0` và p95 latency. | Serving operator |
| 7 | Đo RTO/RPO và postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid:true`, `warnings:[]`, `rto_verdict:PASS`; ghi số đo và các dòng log vào `reports/rto-evidence.md` và `reports/postmortem.md`. | Incident commander |

**Rollback:** Chỉ incident commander cho phép chuyển về A khi A đã được restore, `/readyz` 200 ổn định qua ít nhất ba lần probe, dữ liệu A đã đối chiếu với B, và chuyển về không gây mất các document được ghi ở B. Nếu B vẫn serve tốt thì giữ traffic ở B để tránh flapping. Trước tiên chạy `python3 chaos/kill_region.py restore --region a --backend bare` và kiểm tra A. Khi được phê duyệt, tạm dừng ghi vào B, chạy `python3 state/snapshot.py put --region b --backend fs`, rồi `python3 dr/failover.py --target a --backend fs`; lệnh sau restore từ snapshot B, chờ A ready và mới cutover. Kiểm tra `/edge/state` và 10 request inference. Nếu B lỗi trước khi A đủ điều kiện, incident commander điều phối khôi phục A và báo tiếp diễn outage; không sửa `edge/active_region` thủ công.
