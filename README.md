# GateAI

AI Gateway cho nhiều project: FastAPI đứng trước LiteLLM, OpenRouter cho chat, fal cho media và Runpod Serverless cho model custom. Postgres/Neon giữ ledger và trạng thái job; Redis giới hạn request và đánh thức worker. Một project tương ứng một account và API key riêng.

## Đã có trong MVP

- `/v1/chat/completions`, `/v1/responses`: proxy realtime, text-only; chat hỗ trợ SSE streaming, Responses chưa hỗ trợ streaming; cần `Idempotency-Key`. Đây là tập con API OpenAI, chưa hỗ trợ toàn bộ tham số của SDK.
- `/v1/image`, `/v1/video`, `/v1/tts`, `/v1/stt`, `/v1/custom`: nhận job và trả HTTP 202; `/v1/jobs/{id}` để poll.
- Alias model server-side; client không được ghi đè model thật, số ảnh, độ dài output hay thông số tính phí cố định.
- API key lưu SHA-256; thu hồi key, nạp credit có reference chống trùng, rate limit theo project.
- Reserve credit trong transaction; khóa admission toàn cục; budget ngày UTC theo project và toàn hệ thống. Đơn vị tiền là USD micro-unit (1 USD = 1,000,000).
- Idempotency so sánh cả nội dung request; ledger hoàn tiền đúng một lần; job chỉ đọc được bởi project sở hữu.
- Worker fal queue / Runpod `/run` + `/status`; lưu provider ID trước khi poll. Không có webhook public.
- Redis notification queue + Postgres durable queue: mất notification vẫn tìm lại được job. Có thể chạy nhiều worker bằng row locks; chưa tối ưu throughput lớn.
- Copy media sang private R2/S3 nếu đã cấu hình; URL ký hạn 15 phút được cấp khi chủ job đọc kết quả. Giới hạn file 100 MiB, không theo redirect, chỉ tải từ host provider cho phép.
- Dashboard tại `/` và `/admin/stats`: chi phí trần/doanh thu tháng, hàng đợi, model/provider, job gần đây.
- CI, test Postgres thật trong schema tạm riêng; không gọi API trả phí khi test.

## Chạy trên VPS

Cần Docker Engine + Compose, 4 vCPU / 8 GB RAM là cấu hình khởi đầu dự kiến. Postgres dùng Neon; Redis và LiteLLM chỉ ở mạng nội bộ Compose.

```sh
git clone https://github.com/nhannguyenalien/gateai.git
cd gateai
cp .env.example .env
chmod 600 .env
# Điền DATABASE_URL và các secret trong .env bằng editor trên máy chủ.
# Sinh ADMIN_KEY và LITELLM_MASTER_KEY riêng, ngẫu nhiên ít nhất 32 byte.
docker compose up -d --build
curl http://127.0.0.1:8000/health/ready
```

Migration tạo schema `gateai` và các bảng, không sửa bảng ngoài schema đó. Service `migrate` chạy trước API/worker. Script này là bootstrap idempotent cho phiên bản đầu; khi thay đổi schema cần migration versioned riêng.

API chỉ bind `127.0.0.1:8000`. Đặt Cloudflare Tunnel trỏ `ai-gateway.schoolsai.work` vào địa chỉ này, hoặc reverse proxy TLS trên VPS. Bảo vệ `/admin/*`, `/docs` bằng Cloudflare Access; đặt rate limit IP ở Cloudflare. Tách database và `.env` cho dev/staging/prod. Không publish Redis/LiteLLM trực tiếp ra Internet.

## Bật provider và định giá

Tất cả route mặc định **tắt**. Giá trong `config/routes.yaml` là ví dụ, không phải báo giá nhà cung cấp.

1. Điền `OPENROUTER_API_KEY` và `CHAT_FAST_MODEL`, `CHAT_SMART_MODEL` dạng `openrouter/<model-id>` vào `.env`.
2. Điền `FAL_KEY` / `RUNPOD_API_KEY` khi dùng media hoặc custom endpoint.
3. Chọn endpoint thật, kiểm tra input schema và giá của endpoint. Thay các giá trị `REPLACE_...`.
4. Đặt `fixed`, `allowed_inputs`, giới hạn input, `max_cost_micros`, `user_price_micros`, rồi đổi `enabled: true`.
5. `max_cost_micros` phải ≤ 60% `user_price_micros`. Với video/audio phải khóa thời lượng/độ phân giải theo schema provider trước khi mở cho khách; không bật STT chỉ dựa vào kích thước URL vì URL có thể trỏ tới audio dài.
6. Restart API/worker/LiteLLM sau khi đổi `.env`. Job đã nhận lưu snapshot route, không đổi giá giữa chừng.

Budget là **trần chi phí dự phòng lúc nhận job**, không phải số tiền hóa đơn đã đối soát. Chi phí thực phụ thuộc giá vendor và cấu hình route chính xác; cần đặt thêm spending limit tại provider. Job qua ngày vẫn giữ reservation ở ngày nhận. Gateway hiện bán giá cố định mỗi request, chưa tính lại theo token usage thực.

## Tạo project, nạp credit và gọi API

Xuất `ADMIN_KEY` từ nơi lưu secret vào shell; không chép secret vào lệnh lưu trong shell history.

```sh
curl http://localhost:8000/admin/accounts \
  -H "Authorization: Bearer $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"name":"schoolsai","daily_cap_micros":10000000,"rpm":60}'
```

Lưu `account_id` và `api_key` từ response. Key chỉ trả một lần, đặt `PROJECT_KEY` trong backend của ứng dụng.

```sh
curl "http://localhost:8000/admin/accounts/$ACCOUNT_ID/credits" \
  -H "Authorization: Bearer $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"amount_micros":10000000,"reference":"initial-topup-001"}'

curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $PROJECT_KEY" -H 'Idempotency-Key: chat-demo-001' \
  -H 'Content-Type: application/json' \
  -d '{"model":"chat-fast","messages":[{"role":"user","content":"Xin chào"}]}'

curl http://localhost:8000/v1/image \
  -H "Authorization: Bearer $PROJECT_KEY" -H 'Idempotency-Key: image-demo-001' \
  -H 'Content-Type: application/json' \
  -d '{"model":"image-standard","input":{"prompt":"A quiet Vietnamese classroom"}}'

curl "http://localhost:8000/v1/jobs/$JOB_ID" -H "Authorization: Bearer $PROJECT_KEY"
```

Cùng key + cùng payload trả lại cùng job; khác payload trả 409. Với chat đang chạy hoặc không thành công, retry trả 409 và ID để tra cứu.

## R2/S3

Điền nhóm `S3_*` trong `.env`; bucket private, credential chỉ được đọc/ghi bucket này. `ASSET_ALLOWED_HOSTS` là danh sách host storage của provider do operator tin cậy (mặc định `fal.media`, bao gồm subdomain). Worker bỏ qua redirect và từ chối URL ngoài danh sách. Runpod custom nên trả URL trong storage host đã duyệt, không nhúng file lớn/base64. Nếu bỏ trống bucket, response giữ URL của provider, có thể hết hạn và chưa có signed URL của GateAI.

## Runpod

MVP kết nối **Serverless endpoint sẵn có**, không tự tạo/xóa Pod GPU. Cấu hình endpoint ở Runpod: min workers = 0, idle timeout = 300 giây, max workers giới hạn theo ngân sách, execution timeout theo workload; gắn Network Volume nếu model cần. Chọn GPU theo VRAM/model và availability thực tế (A6000 48 GB là lựa chọn bắt đầu trong kế hoạch). Gateway không bảo đảm cấu hình lifecycle của endpoint bên ngoài.

## Sự cố và đối soát

`queued → submitting → running → succeeded/failed`. Chat đi từ `submitting` thẳng sang `succeeded`. Lỗi submission hoặc worker chết trong lúc gửi trở thành `needs_review`, giữ credit để tránh chạy/tính tiền hai lần. Gateway không tự retry paid submission (retry_count = 0); GET poll có thể retry. Provider có thể có retry nội bộ riêng.

Job `failed` hoàn credit; vẫn giữ cost reservation ngày vì vendor có thể tính tiền lỗi. `needs_review` chỉ đối soát sau khi kiểm tra provider dashboard:

```sh
uv run python -m scripts.reconcile JOB_UUID --outcome failed --note 'Verified provider did not deliver output'
```

S3 upload hoặc polling lỗi sẽ được thử lại, không submit model mới. Nếu provider hết thời hạn giữ kết quả, cần operator đối soát. Hiện chưa có giao diện tự động giải quyết dispute, timeout job tổng thể hay đối chiếu toàn bộ hóa đơn vendor; monitor đối soát chi phí từng request OpenRouter/fal.

## Kiểm thử

```sh
uv sync --frozen
uv run python -m app.db
uv run ruff check .
uv run pytest -q
```

Tests tự tạo và xóa `gateai_test_<uuid>` trên DB cấu hình, cần quyền tạo schema. Kiểm thử concurrency/idempotency/credit/budget/isolation, worker thành công và timeout, auth, URL storage và route responses. Không gọi fal/OpenRouter/Runpod thật.

## Các bước triển khai còn cần cấu hình

- VPS/SSH, DNS/Cloudflare Tunnel, provider key và private bucket chưa được provision bởi repo này.
- Streaming, JWT/end-user rate limit, Telegram alerts, actual token cost settlement, GPU runtime metrics và tự provisioning Pod là phần mở rộng tiếp theo.
- Dashboard hiện hiển thị cost bound, không gọi đó là chi phí thực hay gross margin thực.
- Chưa có OpenMeter, payment gateway hoặc thanh toán tự động.

Tài liệu giao thức: [LiteLLM production](https://docs.litellm.ai/docs/proxy/deploy), [fal queue](https://fal.ai/docs/documentation/model-apis/inference/queue), [Runpod requests](https://docs.runpod.io/serverless/endpoints/send-requests).

## Coolify

Xem [hướng dẫn triển khai Coolify](docs/coolify.md), dùng `compose.coolify.yaml`.

### Streaming, free testing, cost reconciliation, alerts

The global admission budget defaults to **$20 per UTC day** (07:00 Vietnam reset).
It reserves maximum configured provider cost before submission; actual overruns increase
that reservation and raise an alert. This is a gateway admission cap, not a provider-side
spending guarantee. Configure provider account limits too before enabling paid routes.

`chat-free` maps exclusively to `nvidia/nemotron-3-ultra-550b-a55b:free` through OpenRouter,
with no paid fallback. It accepts `stream: true` at `/v1/chat/completions`; keep the
`Idempotency-Key` header. Streaming responses include `X-Job-ID`. Reusing a streaming
key returns 409 with the stored job ID; fetch `/v1/jobs/{id}` instead of resubmitting.
Interrupted streams remain `needs_review`. Only send synthetic/public content to this
free endpoint: its provider records inputs. Free capacity and quotas can limit availability.

Run `GATEWAY_URL=https://your-gateway GATEWAY_API_KEY=... uv run python scripts/smoke_free.py`
using a project key (never the admin key). It checks non-streaming, idempotent replay,
and complete SSE termination without submitting any paid media request.

The `monitor` service polls OpenRouter generation receipts and fal billing events by
request ID. `actual_cost` stays null until a matching receipt arrives; estimates are
never presented as actual costs. Dashboard stats expose reconciliation coverage.
Fal billing access may require a suitably scoped key; missing/ambiguous receipts stay
pending. This does not reconcile Runpod or non-OpenRouter upstreams behind LiteLLM.

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in Coolify and redeploy to enable delivery.
Alerts cover 80% of the daily budget, provider error spikes, and cost overruns. They are
persisted, deduplicated, and retried after five minutes, and contain no prompts or keys.
A delivery acknowledgement lost in transit can still result in a duplicate notification.
Unfunded fal accounts cannot complete paid image/video E2E tests; paid routes remain disabled.
