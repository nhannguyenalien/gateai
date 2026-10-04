# GateAI — tài liệu tích hợp cho dev

Base URL production: `https://ai-gateway.schoolsai.work/v1`.
Swagger: <https://ai-gateway.schoolsai.work/docs> (request chat là JSON mở;
tham số được hỗ trợ được mô tả bên dưới).

## Xác thực và model

Backend app dùng project API key `gai_...` qua `Authorization: Bearer <key>`.
Admin cấp key riêng cho mỗi project. Không dùng ADMIN_KEY/OpenRouter key để gọi
API project, không đặt key vào frontend, ứng dụng mobile hoặc Git.

| `model` gửi vào gateway | Upstream | Trạng thái |
| --- | --- | --- |
| `chat-free` | `nvidia/nemotron-3-ultra-550b-a55b:free` | Bật, text và streaming |
| `chat-inkling-free` | `thinkingmachines/inkling:free` | Đã cấu hình nhưng TẮT: OpenRouter trả 403 yêu cầu agentic harness |

Chỉ gửi alias, không gửi ID upstream. Không tự fallback sang model trả phí.
Danh sách bật tại thời điểm gọi:

```bash
export GATEWAY_URL='https://ai-gateway.schoolsai.work'
# Cấu hình GATEWAY_API_KEY trong secret manager/environment của backend.
curl --fail-with-body -sS "$GATEWAY_URL/v1/models" \
  -H "Authorization: Bearer $GATEWAY_API_KEY"
```

Response có dạng `{"object":"list","data":[{"id":"chat-free","object":"model",...}]}`.
Các trường bổ sung gồm `kind`, `upstream_model`, `streaming`, `max_input_bytes`,
`max_output_tokens`. Danh sách bật không bảo đảm provider luôn còn quota/capacity.

Endpoint miễn phí chỉ dùng dữ liệu công khai/giả lập. NVIDIA ghi lại dữ liệu theo
điều khoản trial. [Inkling free](https://openrouter.ai/thinkingmachines/inkling:free)
chỉ dành cho **agentic harnesses**, ghi lại prompts/outputs để cải thiện sản phẩm;
không gửi dữ liệu cá nhân hoặc bí mật. Gateway hiện chỉ hỗ trợ text, chưa hỗ trợ
`tools`, tool messages, ảnh/audio đầu vào dù upstream có khả năng đó.

## Chat không streaming

```bash
curl --fail-with-body -sS "$GATEWAY_URL/v1/chat/completions" \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"model":"chat-free","stream":false,"messages":[{"role":"user","content":"Hello"}]}'
```

Body chỉ nhận `model`, `messages`, `stream` (boolean, mặc định false).
`messages` là mảng không rỗng; mỗi phần tử chỉ có `role` (`system`, `user`,
`assistant`) và `content` dạng string. Không nhận `temperature`, `max_tokens`,
`tools`, `response_format` hoặc trường bổ sung. Gateway cố định output tối đa
1.024 tokens và giới hạn JSON payload sau khi bỏ `model` ở 12.000 bytes.
Không thể sử dụng toàn bộ context window upstream qua hai alias này.

HTTP 200 trả JSON chat completion của provider: đọc
`choices[0].message.content`; `usage` có thể chứa tokens và `cost` USD.
`id` trong completion là generation ID của provider, không phải UUID job gateway.

## Streaming SSE

```bash
curl -N --fail-with-body -sS "$GATEWAY_URL/v1/chat/completions" \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"model":"chat-free","stream":true,"messages":[{"role":"user","content":"Hello"}]}'
```

HTTP 200, `Content-Type: text/event-stream`; header `X-Job-ID` là UUID job gateway.
Parse từng event SSE (có thể chia qua nhiều network chunks); bỏ qua comment
heartbeat. Ghép `choices[].delta.content`. `usage` có thể ở event cuối riêng.
Chỉ coi stream hoàn tất khi có terminal finish và `data: [DONE]`;
HTTP 200 đơn thuần chưa chứng minh inference thành công. Kiểm tra event `error`.
Nếu mất kết nối/stream bị cắt, job giữ `needs_review`, không tự submit lại.

## Idempotency, lỗi và tra cứu job

Mọi POST inference bắt buộc có `Idempotency-Key` dài 8–150 ký tự.
Tạo key mới cho mỗi yêu cầu logic; giữ nguyên key và body khi retry cùng yêu cầu.
Không dùng key mới chỉ vì timeout vì có thể tạo thêm tác vụ upstream.

- Non-stream: replay thành công trả lại JSON đã lưu, không gọi provider lần nữa.
- Stream: replay trả HTTP 409 kèm `id`/`status`, không replay SSE.
- Cùng key nhưng khác body/model gây 409.
- Khi có job ID từ header/lỗi, truy vấn bằng đúng project key:

```bash
curl --fail-with-body -sS "$GATEWAY_URL/v1/jobs/$JOB_ID" \
  -H "Authorization: Bearer $GATEWAY_API_KEY"
curl --fail-with-body -sS "$GATEWAY_URL/v1/balance" \
  -H "Authorization: Bearer $GATEWAY_API_KEY"
```

Job trả `id`, `kind`, `alias`, `provider`, `status`, `result`, `error`,
`estimated_cost`, `max_cost`, `user_price`, `retry_count`, `created_at`.
Các trường tiền của gateway là **micro-USD**: 1 USD = 1.000.000.
`max_cost`/`estimated_cost` là dự phòng, không phải chi phí thực.
Chi phí thực đối soát hiển thị ở `/admin/stats`; `usage.cost` upstream là USD.

| HTTP | Cách xử lý |
| --- | --- |
| 400 | Alias tắt/không tồn tại hoặc tham số không hỗ trợ |
| 401 | Key sai, thiếu hoặc bị thu hồi |
| 402 | Không đủ credit cho route trả phí |
| 404 | Job không tồn tại hoặc không thuộc project |
| 409 | Idempotency conflict, job đang chạy hoặc replay streaming; tra cứu job |
| 413 | Payload quá lớn |
| 422 | Thiếu header/body hoặc sai kiểu dữ liệu |
| 429 | Rate limit hoặc daily budget; không retry liên tục |
| 502 | Provider lỗi/kết quả chưa xác định; đọc `detail.job_id` và tra cứu |
| 503 | Dependency/provider chưa cấu hình hoặc route vi phạm cost policy |

Rate limit theo account dùng cửa sổ 60 giây, áp dụng cả GET có xác thực.
Budget toàn hệ thống hiện $20/ngày UTC, reset 07:00 giờ Việt Nam;
project có budget riêng. Đây là trần dự phòng lúc nhận job, không phải cam kết
hóa đơn provider. Các alias free cấu hình giá dự phòng và giá bán $0.

## Các endpoint chưa bật model

`POST /v1/responses` hiện không có alias bật.
`POST /v1/image`, `/v1/video`, `/v1/tts`, `/v1/stt`, `/v1/custom` có giao thức
`{"model":"alias","input":{...}}`, trả 202 + job để poll khi được cấu hình;
hiện tất cả route media/Runpod đều tắt. Dev chưa nên tích hợp như dịch vụ đang sẵn sàng.

## Admin: cấp key cho project (chỉ operator)

```bash
curl --fail-with-body -sS "$GATEWAY_URL/admin/accounts" \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"my-project","daily_cap_micros":5000000,"rpm":60}'
```

Lưu `account_id` và `api_key` trả về vào secret manager; key chỉ trả plaintext lúc tạo.
Account mới có credit 0, dùng được route free. Nạp credit thủ công bằng
`POST /admin/accounts/{id}/credits` với `amount_micros` và `reference` duy nhất
(8–150 ký tự). Thu hồi toàn bộ key account qua `DELETE /admin/accounts/{id}/keys`.
Không cấp ADMIN_KEY cho dev chỉ cần gọi inference.

## Smoke test

```bash
GATEWAY_MODEL=chat-free uv run python scripts/smoke_free.py
```

Cần `GATEWAY_URL` và `GATEWAY_API_KEY` trong environment. Script thử non-stream,
idempotent replay và SSE hoàn tất bằng dữ liệu giả lập. Bỏ `GATEWAY_MODEL` để thử
`chat-free`. Chỉ thử `chat-inkling-free` sau khi provider cấp quyền và operator bật route. Free quota/capacity thay đổi nên kết quả một lượt không đảm bảo SLA.

## Kết quả thử Inkling trực tiếp — 2026-10-04

Đã chạy `curl -N` với đúng model `thinkingmachines/inkling:free`, `stream: true`,
message `Hello` và key server. OpenRouter trả JSON lỗi code **403**, không có SSE:
`only available on agentic harnesses` (routing step: `Gate Free Endpoints by Agentic Harness`).
Vì vậy route này tắt và không xuất hiện trong `/v1/models`. Không giả mạo tên ứng dụng
để vượt điều kiện provider. Cần tích hợp qua harness được chấp nhận hoặc làm việc với
OpenRouter trước khi bật. Nemotron `chat-free` vẫn sử dụng được như các ví dụ trên.
