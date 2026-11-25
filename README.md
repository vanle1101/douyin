# Douyin Studio — local

Giao diện Site riêng tư kết nối với engine chạy trên máy cá nhân. Không tự cài phần mềm hoặc tải model. Video, WAV, SQLite, log và bản xuất nằm trong thư mục `data/` cạnh `app.py`. Giao diện mở được từ Site khi engine tắt, nhưng nút xử lý chỉ hoạt động khi engine cục bộ đang chạy.

## Khởi chạy trên Windows

1. Giải nén toàn bộ ZIP vào ổ bạn muốn dùng, ví dụ `E:\Douyin Studio`. Không đặt ở thư mục chỉ đọc.
2. Nhấp đúp `start-studio.bat`, hoặc mở PowerShell tại thư mục vừa giải nén và chạy `py -3 app.py` (hoặc `python app.py` nếu máy không có `py`). Giữ cửa sổ đó mở trong khi xử lý.
3. Mở `http://127.0.0.1:8765` trên chính máy đó.
4. Vào **Cấu hình** điền đường dẫn công cụ/model đã có trên máy, lưu, rồi xem **Kiểm tra môi trường**. Thêm link hoặc video ở **Hàng đợi**.

Giao diện riêng tư cũng có tại `https://douyin-studio-van-2026.hushedopal1.chatgpt.site`. Link này tải giao diện từ Site và kết nối trực tiếp tới engine `http://127.0.0.1:8765` trong trình duyệt của bạn. Vẫn phải chạy `app.py` trên máy dùng ffmpeg/model; nếu Chrome hỏi quyền truy cập mạng cục bộ, hãy cho phép Site này. Truy cập `127.0.0.1` trực tiếp là cách chắc chắn nhất nếu trình duyệt chặn kết nối từ Site.

Chương trình dùng Python 3.10+ với thư viện chuẩn cho máy chủ và hàng đợi. Các bước xử lý yêu cầu ffmpeg/ffprobe, `faster_whisper`, Ollama đang phục vụ model dịch đã tải sẵn, và Piper cùng giọng `.onnx` + `.onnx.json`. Link Douyin cần yt-dlp; có thể chọn cookies Netscape TXT sẵn có. File video cục bộ không cần yt-dlp. Không có lệnh cài đặt tự động.

## Hai cấu hình máy và chế độ tự động

- **RTX 2050 4 GB / RAM 16 GB:** ảnh cho thấy 14.0 GB đang dùng, còn khoảng 1.7 GB lúc chụp. Chế độ tự động chọn đoạn 120 giây, một luồng công việc ASR CPU với 3 luồng tính toán, ngưỡng RAM trống 3000 MiB. Bước dịch yêu cầu ít nhất 3500 MiB trống. Với ảnh hiện tại, phải đóng bớt ứng dụng đến khi RAM còn khoảng 3.5–4 GB trở lên; nếu chưa đủ, job giữ dữ liệu và dừng bước nặng.
- **GTX 1050 Ti 4 GB / RAM 32 GB:** ảnh cho thấy 10.5/31.9 GB đang dùng lúc chụp. Chế độ tự động chọn đoạn 300 giây, tối đa hai đoạn ASR CPU và 4 luồng mỗi đoạn, ngưỡng RAM trống 4500 MiB. Máy có RAM trống nhiều hơn, nhưng tính tương thích CUDA của GTX 1050 Ti vẫn cần kiểm tra thực tế. GPU dùng một đoạn mỗi lần và tự chuyển CPU nếu chính sách `auto` cho phép.

Mọi lựa chọn này dựa trên RAM **đo trực tiếp khi engine chạy**, không dựa trên máy đã dùng để tải ZIP. Có thể chọn **Chỉnh tay** trong Cấu hình. Ngưỡng RAM/VRAM là mức bảo vệ thận trọng, không cam kết bất kỳ model nào chạy thành công. Số liệu trong hai ảnh là ảnh chụp một thời điểm, không phải phép đo hiệu năng.

ASR mặc định `small` khi dùng GPU, `base` khi chuyển CPU; cả hai phải có sẵn trong cache local hoặc được cấu hình bằng đường dẫn. Model dịch mặc định là `qwen2.5:1.5b` để giảm tải RAM; **không tự tải**. Chọn tên model Ollama đã có trong danh sách trên trang Cấu hình. Giọng Piper cũng cần trỏ tới `.onnx` đã có. Chưa xác nhận máy nào đã có đủ các model/công cụ đó.

## Pipeline

- Video 30 giây đến 2 giờ. Âm thanh WAV mono 16 kHz chia thành đoạn 120 hoặc 300 giây theo RAM; nếu dùng Chỉnh tay thì chọn 30–600 giây.
- Faster-Whisper `small`, `local_files_only=True`, timestamps theo từ và VAD; GPU `int8_float16` nếu đủ VRAM, tự chuyển CPU `int8` khi chính sách `auto`. CPU xử lý tối đa số đoạn cấu hình cùng lúc qua `num_workers`, tổng luồng ASR không vượt quá 8. GPU chạy một đoạn cùng lúc. Máy chủ xử lý **một video tại một thời điểm**.
- Dịch batch 8 câu bằng Ollama local ở `127.0.0.1`; yêu cầu JSON đủ số câu. Nếu provider thiếu câu hoặc từ chối, job dừng với thông báo `provider_incomplete`, giữ lời thoại để sửa ở **Phụ đề & preview**. Các batch đã xong được lưu.
- Piper được giữ trong một tiến trình suốt bước TTS. Nếu bản Piper CLI không có `--json-input`, worker Python dùng `PiperVoice` để giữ model trong bộ nhớ. Adapter TTS local khác nhận lệnh dạng mảng JSON, ví dụ `["python", "E:\\TTS\\speak.py", "--text", "{text}", "--output", "{output}"]`; nó phải tạo WAV ở `{output}`. Adapter tuỳ chỉnh chạy từng câu.
- Căn timing bằng `atempo`; các câu phải tăng tốc hơn 1.5 lần được ghi vào `timing_notes.json` và log để nghe lại. Bản tiếng Việt được mix với âm gốc ở mức 22%. MP4 xuất bằng x264 + AAC, phụ đề mềm `mov_text`; SRT và WebVTT cũng được tạo. Muốn phụ đề cháy cứng cần bổ sung một preset ffmpeg riêng.
- Nút **Dừng** kết thúc tiến trình con đang chạy và giữ file trung gian. **Tiếp tục** chạy lại từ bước chưa xong. **Chạy lại từ đây** đặt lại các bước sau, làm mới bản dịch hoặc giọng nếu cần. Một số bước dài chỉ có tiến độ theo bước hoặc theo đoạn trong log, không có phần trăm nội bộ của ffmpeg/Ollama.

## Dữ liệu và giới hạn

- `data/studio.sqlite3`: cấu hình, hàng đợi, lịch sử phiên. `data/jobs/<id>/`: video đầu vào, đoạn âm thanh, câu thoại, phụ đề, WAV, output và log. `data/uploads/`: bản sao file tải từ trình duyệt. Các output hoàn tất được giữ theo mã phiên để so sánh và tải lại. Cần tự kiểm soát dung lượng đĩa, nhất là video dài.
- Trang chỉ bind `127.0.0.1`, API thay đổi trạng thái yêu cầu header cùng nguồn gốc. Không dùng dịch vụ dịch trả phí. Đưa link Douyin sẽ kết nối Douyin để tải video; file video cục bộ và Ollama local không gửi video ra ngoài.
- Ngưỡng VRAM chỉ là điều kiện ban đầu; tính tương thích CUDA/CTranslate2 với driver GPU của máy đang chạy phải được kiểm chứng bằng chạy thật. Không có model tải sẵn trong ZIP; model ASR/giọng không tìm được sẽ chặn bước tương ứng.
- Chưa tích hợp vào repo dịch video hiện có trên máy Windows, vì mã nguồn và đường dẫn thực của repo đó không nằm trong workspace này. Chưa kiểm thử end-to-end bằng một video Douyin và các model trên chính máy của bạn.

## Kiểm tra đã thực hiện

Đã kiểm tra cú pháp Python/JavaScript; HTTP API cho upload, tạo job và chạy tới bước nhận diện; ffmpeg tách âm; tạo SRT; căn timing; ghép MP4 với phụ đề. Video tổng hợp 30.2 giây giữ nguyên thời lượng 30.2 giây sau ghép. ASR, Ollama, Piper và GPU chưa được thử trong môi trường tạo Site vì các công cụ/model đó không có ở đây.
