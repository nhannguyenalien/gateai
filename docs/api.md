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

Có thể gửi alias hoặc ID upstream chính xác của model đang bật. Không tự fallback sang model trả phí.
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
không gửi dữ liệu cá nhân hoặc bí mật. Gateway chuyển tiếp tools, tool messages và content parts ảnh/audio/video theo khả năng model.

## Khả năng đã kiểm thử

Kiểm thử production ngày 05/10/2026: chat-liquid-free đã qua tool calling hai lượt, streaming tool calls và JSON Schema. chat-apodex-free đã qua Responses JSON/SSE nhưng provider từ chối tool_choice=required (404) và request JSON Schema đã thử (400). Khả năng có thể thay đổi theo endpoint; dùng chat-liquid-free cho ví dụ tool trên trang /models.

## Chat không streaming

```bash
curl --fail-with-body -sS "$GATEWAY_URL/v1/chat/completions" \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"model":"chat-free","stream":false,"messages":[{"role":"user","content":"Hello"}]}'
```

Chat nhận messages (role system/developer/user/assistant/tool), tools, tool_choice,
parallel_tool_calls, response_format, temperature, top_p, stop, seed, reasoning,
max_tokens hoặc max_completion_tokens. Giữ nguyên tool_calls/reasoning_details
trong lịch sử và gửi kết quả bằng role tool + tool_call_id. Content nhận string
hoặc parts text/image_url/input_audio/video_url tùy model. Provider phải hỗ trợ
các tham số yêu cầu; không suy ra mọi model có cùng khả năng.

Giới hạn JSON 4 MiB; output mặc định 4096, tùy chỉnh đến 32768 token (còn chịu
context/output limit provider). Chỉ n=1. Không nhận provider/models/plugins,
server tools, file parsing hoặc paid fallback từ client.

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

`Idempotency-Key` tùy chọn với chat/responses/embeddings; nếu gửi dài 8–150 ký tự. Bỏ qua sẽ tạo job mới mỗi lần gọi. Media vẫn bắt buộc header này.
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

`POST /v1/responses` dùng cùng các chat alias bật: input string hoặc items,
instructions, function tools, text.format, reasoning, max_output_tokens, stream.
Stateless: gửi lịch sử mỗi lần; chưa hỗ trợ previous_response_id, store:true,
background:true hoặc GET/DELETE response. Stream giữ event type và kết thúc bằng
response.completed/response.incomplete; đọc status, không coi incomplete là trả lời đầy đủ.
Chat stream giữ nguyên delta.tool_calls và reasoning_details; ghép tool arguments
theo index. Gateway lưu kết quả tool calls khi stream hoàn tất.

Computer use cần agent runtime/browser/desktop sandbox bên ứng dụng. Có thể dùng
function tools để thực thi click/type/screenshot, rồi gửi kết quả về model có vision.
Gateway không cung cấp máy tính và chưa hỗ trợ computer_use riêng của vendor.
Chưa hỗ trợ Realtime/Files/Batch/fine-tuning/web search. Stream giới hạn 180 giây,
2 triệu ký tự, 256 KiB/event; lỗi hoặc disconnect giữ job needs_review.

Ví dụ SDK, tools và Responses tại https://ai-gateway.schoolsai.work/models.

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

## Playground và embedding free

Mở https://ai-gateway.schoolsai.work/models, nhập **project API key** để tải danh sách,
chọn ID và test. Không nhập ADMIN_KEY/provider key. Key chỉ giữ trong bộ nhớ trang.
GET /v1/models trả thêm endpoint, dimensions và giới hạn input cho dev chọn đúng API.

| Gateway ID | Upstream ID | Chiều |
|---|---|---|
| embed-nemotron-free | nvidia/nemotron-3-embed-1b:free | 2048 |
| embed-nemotron-vl-free | nvidia/llama-nemotron-embed-vl-1b-v2:free | 2048 |
| embed-liquid-free | liquid/lfm-2.5-embedding-350m:free | 1024 |

POST /v1/embeddings nhận input là chuỗi hoặc mảng 1–16 chuỗi không rỗng.
Nhận text và encoding_format float/base64. dimensions nếu gửi phải đúng số chiều cấu hình; chưa hỗ trợ ảnh/token IDs.
Giới hạn JSON: Liquid 1500 bytes; NVIDIA 12000 bytes. Đây là giới hạn gateway,
không phải token context: Liquid có context 512 token, provider vẫn có thể từ chối
input vượt giới hạn token. Dùng cùng model khi index và query; không trộn vector
từ các model khác nhau, kể cả cùng số chiều.

```bash
curl https://ai-gateway.schoolsai.work/v1/embeddings \
  -H "Authorization: Bearer $GATEAI_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"model":"embed-liquid-free","input":["Bút chì đỏ","Bút chì xanh"]}'
```

Chat mới: chat-apodex-free, chat-laguna-free, chat-nano-free, chat-liquid-free.
Dùng /v1/chat/completions, hỗ trợ stream:true như chat-free.
Laguna free dự kiến ngừng ngày 31/10/2026. Free có rate limit/provider availability;
không tự fallback sang model trả phí. Inkling/Gemma chưa bật do lỗi quyền/quota
trong lần test. Mercury dùng API riêng nên chưa đưa vào chat endpoint.

Chỉ gửi dữ liệu mẫu không nhạy cảm tới free provider; có thể có logging/training
theo điều khoản từng provider. Kết quả test thành công không bảo đảm SLA.
