# Triển khai trên Coolify

Tạo Application từ Git repository `https://github.com/nhannguyenalien/gateai`, branch `main`, build pack **Docker Compose**. Base Directory `/`, Docker Compose Location `/compose.coolify.yaml`. Nếu repo private, kết nối GitHub App hoặc deploy key có quyền đọc repo.

Trong Environment Variables của Coolify, đặt `DATABASE_URL` (Neon có SSL), `ADMIN_KEY` và `LITELLM_MASTER_KEY`. Tạo secret riêng cho môi trường production; không nhập secret vào Git. ADMIN_KEY có thể tạo bằng `openssl rand -hex 32`; LITELLM_MASTER_KEY dùng tiền tố `sk-` cộng chuỗi ngẫu nhiên. Các biến provider và R2/S3 xem `.env.example`; chỉ cần cấu hình khi bật tính năng tương ứng. Không cần upload `.env` lên Git hoặc server.

Load Compose rồi gán domain **chỉ cho service api**: `https://ai-gateway.schoolsai.work:8000`. Port 8000 là port nội bộ để Coolify proxy tới; URL người dùng là `https://ai-gateway.schoolsai.work`. Trỏ DNS domain tới server/proxy phù hợp trước khi deploy. Giữ chế độ Compose thông thường để Coolify quản lý proxy/TLS. Không gán domain cho worker, migrate, Redis hoặc LiteLLM.

Deploy. Service migrate chạy một lần và thoát mã 0 là bình thường. API và worker chờ migration thành công, Redis healthy. Redis có volume bền vững; dữ liệu tài khoản, credit và job nằm ở Neon. LiteLLM config được đóng gói trong image, không phụ thuộc file bind mount trên host.

Kiểm tra `/health/live`, `/health/ready` và dashboard `/`. Dùng ADMIN_KEY để xem dashboard và tạo project/key theo README. Health ready kiểm tra Postgres/Redis; nó không chứng minh provider đã sẵn sàng. Mọi model alias mặc định bị tắt. Điền provider keys, chọn model thật, kiểm tra giá/giới hạn rồi bật route trong `config/routes.yaml`, push và redeploy trước khi gọi AI.

Không scale API/worker trước khi xác minh tài nguyên server và tải thực tế. Chưa có provisioning GPU tự động. Khi chưa có provider key, hạ tầng có thể chạy nhưng chưa tạo được nội dung AI.

Tài liệu chính thức: https://coolify.io/docs/applications/builds/docker-compose
