# Postmortem — Day 23 DR drill

Ngày 2026-10-09 UTC, chúng tôi chủ động gây `netblock --mock` cho Region A khi traffic đang chạy. Baseline không có cơ chế khôi phục: 16 request sau outage lỗi và RTO là `NO_RECOVERY` (`chaos/chaos-events.jsonl:1`, `reports/drill-1-nodr.jsonl:18`, `reports/drill-1-nodr.jsonl:33`). Trong lần có DR, health checker phát hiện sau ba lần probe lỗi, runbook restore snapshot và chỉ đổi edge sang B khi B ready.

## Timeline

| UTC | Sự kiện | Evidence |
|---|---|---|
| 12:40:56.677 | Region A bị tạm dừng, Region B vẫn alive | `chaos/chaos-events.jsonl:3` |
| 12:40:58.694 | Request đầu tiên bắt đầu sau kill trả 503 | `reports/drill-2-withdr.jsonl:26` |
| 12:41:11.648 | Health checker báo A UNHEALTHY sau 3 lỗi liên tiếp | `reports/health-events.jsonl:2` |
| 12:41:11.923 | Operator/runbook ghi nhận incident, trễ 15.25s từ outage | `reports/runbook-run.jsonl:2` |
| 12:41:11.942 | Snapshot phục hồi; RPO 6.0s / 3 document | `reports/failover-events.jsonl:2` |
| 12:41:18.094 | B ready sau 6.15s warm-up | `reports/failover-events.jsonl:4` |
| 12:41:18.107 | Edge pointer chuyển sang B | `reports/failover-events.jsonl:5` |
| 12:41:18.841 | Request đầu tiên thành công từ B, resolved | `reports/drill-2-withdr.jsonl:36` |

## RTO/RPO và gap analysis

RTO mục tiêu 300s, đo được **22.2s**, dư địa **277.8s**. RPO mục tiêu 300s, đo được **6.0s / 3 document**, dư địa **294.0s** (`reports/failover-events.jsonl:2`, `reports/drill-2-withdr.jsonl:36`). Phép đo trả `valid:true`, `warnings:[]`, `rto_verdict:PASS`; golden signals của B là 10 request, error rate 0, p95 19.8ms (`reports/runbook-run.jsonl:6`).

**Gap lớn nhất trong RTO:** phát hiện outage mất 14.97s, khoảng 67.4% RTO. Cấu hình `interval=5s`, `threshold=3` đặt detect floor 15s (`reports/health-events.jsonl:2`). Snapshot restore và xác minh chiếm 0.29s; warm-up 6.15s; đoạn từ B ready tới request thành công 0.75s. Tổng theo timestamp là 22.16s, khớp RTO làm tròn 22.2s. Trong baseline, không có health checker/runbook/cutover nên sau 30.7s quan sát vẫn không phục hồi (`reports/drill-1-nodr.jsonl:33`).

## Root cause — 5 whys

1. Vì sao user nhận 503? Edge tiếp tục trỏ tới A sau khi A ngừng trả lời.
2. Vì sao edge chưa đổi ngay? Cutover phải đợi tín hiệu A lỗi liên tiếp và B đủ điều kiện phục vụ.
3. Vì sao B chưa đủ điều kiện? Ban đầu B thiếu vector DB, model weights và pool ở trạng thái warm (`reports/runbook-run.jsonl:1`).
4. Vì sao cần restore trong outage? Dữ liệu chỉ được snapshot định kỳ, chưa được đồng bộ liên tục sang B (`reports/replication.jsonl:2`).
5. Vì sao có RPO? Ba document mới trên A sau snapshot cuối không có trong bản restore (`reports/failover-events.jsonl:2`).

Không có lỗi thao tác cá nhân: thời gian chờ được tạo bởi chính ngưỡng phát hiện, cơ chế snapshot định kỳ và GPU warm-up. Điểm có thể làm hỏng runbook trong outage thật là snapshot chưa tồn tại hoặc B không đạt `/readyz`; khi đó quy trình phải abort trước cutover.

## Action items

| # | Action | Owner | Deadline (Asia/Bangkok) | Tác động dự kiến |
|---|---|---|---|---|
| 1 | Thử interval 3s, threshold 3 trong 5 lần game day; chỉ giữ nếu không tăng false alert | SRE on-call | 2026-10-12 | Giảm floor lý thuyết 6s; phải đo lại RTO thực tế. |
| 2 | Đánh giá chi phí giữ B ở pool full và snapshot/replication thường xuyên hơn | Platform owner | 2026-10-16 | Có thể loại bỏ ~6.15s warm-up; lịch snapshot ngắn hơn có thể giảm 6.0s RPO quan sát được. |

## Trả lời câu hỏi bắt buộc

1. `interval × threshold = 5 × 3 = 15s`, tương đương khoảng **67.6%** của RTO 22.2s. Thực đo detection là 14.97s, tương đương **67.4%**. Với mục tiêu RTO 300s và các pha khác giữ ở ~7.2s, interval tối đa về mặt số học là khoảng 97.6s khi threshold=3; thực tế phải chừa biên cho timeout, tải và sai lệch TTL.
2. Nếu interval là 1s, floor lý thuyết từ 15s xuống 3s, tức tối đa **12s** cải thiện. Với `netblock`, timeout probe 2s có thể chi phối tốc độ poll, nên không thể coi 12s là mức giảm RTO đã được chứng minh. Ba lần fail trong 3s cũng dễ bắt nhầm dao động ngắn; cần đo false alert trước khi đổi.
3. Nếu A mất vĩnh viễn, `docs_lost=3` nghĩa là ba document khách hàng đã ghi ở A nhưng không có trong B tại lúc restore. Cần replay từ nguồn ghi độc lập nếu có; nếu không, đây là mất dữ liệu thật, không phải chỉ số trễ kỹ thuật.
