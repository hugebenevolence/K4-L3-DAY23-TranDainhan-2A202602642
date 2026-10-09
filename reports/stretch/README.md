# Day 23 — sáu mục mở rộng

Các thử nghiệm dưới đây chạy trên state tạm riêng. Log và báo cáo của drill bắt buộc ở thư mục `reports/` không bị viết thêm. Các số thời gian là một lần đo trên máy này, không phải cam kết hiệu năng.

## 1. MinIO thật

`docker compose up -d minio` đã khởi động dịch vụ trên cổng 9000/9001. Image MinIO community trong đề không còn pull công khai được; Compose đã pin image mirror build từ [mã nguồn MinIO được lưu trữ](https://github.com/minio/minio) bằng digest để bài lab chạy lại được. Mirror chỉ dùng cho thử nghiệm cục bộ. [Tài liệu MinIO](https://min.io/docs/minio/container/operations/install-deploy-manage/deploy-minio-single-node-single-drive.html) mô tả triển khai một node để phát triển và đánh giá.

Chạy so sánh bằng `MINIO_URL=http://127.0.0.1:9000 python stretch/minio_compare.py` trong shell Linux, hoặc đặt biến `MINIO_URL` tương đương trong PowerShell. Script tạo bucket, đo `put/get` của cả hai backend, gọi failover qua MinIO, và xác nhận 10 request từ B. Kết quả gốc ở [`minio.json`](minio.json): FS put/get **28.70/20.82 ms**, MinIO put/get **172.45/78.96 ms**, failover thành công. Chênh lệch phản ánh API S3 và Docker trên máy này; cần nhiều lần đo mới suy ra phân phối độ trễ.

## 2. PostgreSQL PITR

Mô hình dùng PostgreSQL 16 với `wal_level=replica`, `archive_mode=on`, `archive_command` ghi vào volume riêng; `pg_basebackup -Fp -Xs` tạo base backup. Script [`stretch/pitr_demo.py`](../../stretch/pitr_demo.py) ghi một hàng trước mốc khôi phục, một hàng sau mốc, buộc đổi WAL bằng `pg_switch_wal()`, rồi khởi động cluster restore với `recovery.signal`, `restore_command` và `recovery_target_time`. Trình tự tuân theo [hướng dẫn PITR PostgreSQL 16](https://www.postgresql.org/docs/16/continuous-archiving.html).

Để tái chạy từ container/volume mới (Docker CLI và Python trên host):

```bash
docker network create day23-stretch
docker volume create day23-pg-archive
docker volume create day23-pg-backup
docker run -d --name day23-postgres --network day23-stretch \
  -e POSTGRES_PASSWORD=labpass -e POSTGRES_HOST_AUTH_METHOD=trust \
  --mount source=day23-pg-archive,target=/archive \
  --mount source=day23-pg-backup,target=/backup postgres:16-alpine \
  sh -c 'chown postgres:postgres /archive /backup; exec docker-entrypoint.sh postgres -c wal_level=replica -c archive_mode=on -c "archive_command=cp %p /archive/%f"'
python stretch/pitr_demo.py
```

Chỉ dùng mật khẩu mẫu này trong mạng Docker lab. Kết quả ở [`postgres-pitr.json`](postgres-pitr.json): bản restore có `baseline` và `before_target`, không có `after_target`; thời gian từ khởi động container restore tới SQL sẵn sàng là **3.83 s**. Đây là RTO riêng lớp metadata PostgreSQL.

## 3. Active-active

Đặt `EDGE_MODE=active_active` để edge chọn A/B luân phiên, đồng bộ bộ đếm giữa các request; mặc định vẫn là active-passive. Hai region đều có weights, vectors và `pool_state=full`. Chạy `python stretch/active_active_demo.py`; [`active-active.json`](active-active.json) ghi **20 request, A=10, B=10, 0 lỗi**.

Với ghi đồng thời cùng document, [`stretch/conflicts.py`](../../stretch/conflicts.py) chọn sự kiện có khóa `(Lamport clock, region, event_id)` lớn nhất; mọi sự kiện thua vẫn được giữ trong danh sách conflict để audit hoặc xử lý tay. Writer phải tăng Lamport clock sau khi thấy sự kiện từ region kia. Đây là quy tắc merge metadata minh họa, chưa thay thế cơ chế đồng bộ vector DB hai chiều hay tự động loại region lỗi khỏi vòng chia tải.

## 4. Terraform — chỉ viết, không apply

[`terraform/replication.tf`](../../terraform/replication.tf) tạo hai bucket khác region, bật versioning ở cả hai, cấp role S3 và một `aws_s3_bucket_replication_configuration` cho toàn bộ object. `vectors.sqlite`, `model.bin` và `MANIFEST.json` của `state/snapshot.py` tương ứng các object được sao chép; S3 versioning giữ phiên bản object, còn `MANIFEST.json` ghi timestamp và `embed_model_version` ở tầng ứng dụng. Cấu hình bám theo [AWS provider resource](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket_replication_configuration). Đã chạy `terraform fmt -check -recursive` và `terraform validate` thành công; chưa `apply`, không dùng tài khoản AWS.

## 5. Randomized chaos — năm lần

[`stretch/randomized_chaos.py`](../../stretch/randomized_chaos.py) dùng seed 23 để xáo trộn `stop`/`netblock` và thời điểm kill 5–8 giây. Mỗi lần có state, process và log riêng; script đo RTO từ log rồi ghi [`randomized-chaos/summary.json`](randomized-chaos/summary.json) cùng log từng lần. Chạy trong Linux với `python stretch/randomized_chaos.py` sau `pip install -r requirements.txt`; Windows Python không có `SIGSTOP`. Cấu hình thử nghiệm: health interval 2s, threshold 3, warm-up 2s, edge TTL 3s, traffic 36s. Các số này khác drill chấm điểm, nên không so trực tiếp RTO giữa hai cấu hình.

| Lần | Mode | Kill sau | RTO | Hợp lệ |
|---:|---|---:|---:|---|
| 1 | netblock | 6.27s | 10.6s | Có |
| 2 | stop | 6.59s | 8.1s | Có |
| 3 | netblock | 5.39s | 10.2s | Có |
| 4 | stop | 5.58s | 6.9s | Có |
| 5 | stop | 6.33s | 9.4s | Có |

**RTO trung bình 9.04s, độ lệch chuẩn mẫu 1.53s.** Cả năm lần `valid:true`, `warnings:[]`, phục hồi từ B. RPO của riêng thử nghiệm này bằng 0 vì không bật ingest liên tục; RPO 6.0s / 3 document của drill bắt buộc vẫn là bằng chứng mất dữ liệu thực tế.

## 6. Tự đánh giá mức trưởng thành DR

**Level 3/4 — đã drill đầu-cuối.** Có bản backup, runbook, hai lần drill bắt buộc và năm lần randomized chaos, đo RTO/RPO bằng log, kiểm tra phục hồi thực sự. `--auto` hiện hỗ trợ CI nhưng chưa có lịch game day và chưa có gate tự động liên tục. Để lên **Level 4**, chạy drill định kỳ trong môi trường riêng, tự động lưu bằng chứng, cảnh báo khi RTO/RPO vượt mục tiêu, và kiểm tra restore backup trước khi cần failover thật. File slide định nghĩa thang Level 0–4 không có trong repo; đánh giá này dùng diễn giải “đã drill” → “tự động kiểm chứng liên tục”, không nhận là trích nguyên văn slide.

## Ảnh nộp bài

`README.md`, `GUIDE.md` và `RUBRIC.md` không yêu cầu ảnh hoặc screenshot. Bằng chứng cần nộp là code, log timestamp và báo cáo Markdown/JSON. Không cần tạo ảnh cho sáu mục mở rộng.
