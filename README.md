# Thu thập bài viết và tài liệu HUST

Công cụ Python khám phá các hostname thuộc `hust.edu.vn`, đi theo liên kết,
đọc sitemap/RSS, bóc tách nội dung và xuất JSONL để đưa vào Lucene.
Chạy từ thư mục gốc của dự án, Python 3.10 trở lên.

## 1. Cài đặt

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Các ví dụ bên dưới dùng `python`; nếu chưa kích hoạt môi trường, thay bằng
`.\.venv\Scripts\python.exe`.

## 2. Tìm miền con

Nhập danh sách hostname/URL (mỗi dòng một mục) từ Subfinder hoặc danh sách tự khảo sát:

```powershell
subfinder -d hust.edu.vn -all -silent -o subfinder.txt
python -m hust_collect discover --input subfinder.txt
```

Hoặc truy vấn Certificate Transparency qua crt.sh:

```powershell
python -m hust_collect discover --crtsh
```

Không cần cài Subfinder nếu dùng crt.sh hoặc danh sách tự tạo. crt.sh có thể lỗi,
timeout hoặc giới hạn truy cập. Script ghi lỗi và giữ các miền đã nhập thành công.
Wildcard `*.hust.edu.vn` không được coi là một hostname cụ thể. Miền trong chứng chỉ
có thể đã ngừng hoạt động. Danh sách phát hiện được không phải tổng số miền của HUST.
Kết quả nhập được lưu trong SQLite và `data/subdomains.txt`; tệp này là ảnh chụp ở
lần chạy `discover`, còn SQLite có thêm các miền phát hiện khi crawl.

## 3. Thu thập thử

Chỉnh `seeds.txt`: thêm trang chủ và các URL danh mục đã khảo sát, mỗi dòng một URL.

```powershell
python -m hust_collect crawl --include-discovered --max-pages 100 --per-domain 10 --max-domains 20
python -m hust_collect stats
```

Nếu đã có file mẫu `all_extracted_results.json` (mảng bản ghi gồm `url`, `title`,
`content`, `olinks`), dùng các URL của nó và các hostname trong `olinks` làm mầm:

```powershell
python -m hust_collect crawl --reference-json "D:\duong-dan\all_extracted_results.json" --max-pages 300 --per-domain 30
```

Lệnh này **không sao chép** `title`/`content` cũ vào kết quả mới. Chỉ khi tải và bóc
tách thành công, tài liệu mới được xuất. Mọi lượt crawl/reprocess/export sinh thêm
`data/all_extracted_results.json`, là mảng JSON có đúng cấu trúc file mẫu:

```json
[
  {
    "url": "https://ten-mien.hust.edu.vn/bai-viet.html",
    "title": "Tiêu đề bài viết",
    "content": "Nội dung chính đã làm sạch",
    "olinks": [
      {"atext": "Văn bản liên kết", "dest": "https://ten-mien.hust.edu.vn/tai-lieu.pdf", "etext": "Văn bản quanh liên kết"}
    ]
  }
]
```

`atext` là chữ trong thẻ liên kết; `dest` là URL tuyệt đối; `etext` là văn bản của
phần tử cha trực tiếp. Giữ thứ tự, liên kết lặp và fragment. Lưu cả liên kết HTTP(S)
ngoài HUST làm bằng chứng, nhưng chỉ theo liên kết trong phạm vi HUST khi crawl.
Không lưu mailto/javascript hoặc URL chứa thông tin đăng nhập. Tài liệu đính kèm
có `olinks: []`. Nội dung chính được làm sạch; không tái tạo phần tin liên quan/menu
bị lẫn trong nội dung của file mẫu. File JSON mẫu đầu vào được giữ nguyên.
Trong mảng JSON, `url` là địa chỉ cuối sau chuyển hướng; các URL cũ cùng chuyển
đến một địa chỉ chỉ xuất một lần. JSONL chi tiết vẫn giữ URL yêu cầu và `final_url`
để theo dõi cập nhật. Do đó số dòng JSONL có thể lớn hơn số bản ghi trong mảng JSON.

- `--include-discovered`: đưa trang chủ các miền trong SQLite vào hàng đợi.
- Crawler tự tìm miền mới qua liên kết trong HTML, kể cả liên kết ở menu/chân trang.
- `--max-pages`: số URL hàng đợi được xử lý mỗi lượt; không phải số bài viết thành công.
  Yêu cầu robots.txt và các chặng chuyển hướng là yêu cầu bổ sung.
- `--per-domain`: giới hạn URL hàng đợi mỗi hostname trong một lượt.
- `--max-domains`: giới hạn số hostname được đưa vào hàng đợi. Các miền vượt giới hạn
  vẫn được ghi nhận để khảo sát sau. Không phải giới hạn tổng hostname phát hiện.
- `--max-depth 6`: giới hạn số bước đi theo liên kết HTML tính từ URL mầm.
- `--max-urls 50000`: giới hạn tổng số URL lưu trong hàng đợi của bộ dữ liệu này.
- `--delay 2`: tối thiểu 2 giây giữa các yêu cầu tới cùng hostname; tăng theo
  `Crawl-delay`/`Request-rate` nếu robots.txt công bố.
- `--max-bytes 20971520`: giới hạn 20 MiB cho mỗi phản hồi sau giải nén HTTP và sitemap gzip.
- `--timeout 20`: timeout kết nối/đọc mỗi yêu cầu, không phải hạn mức toàn bộ lượt chạy.

Mặc định chạy tuần tự, tuân thủ robots.txt, kiểm tra phạm vi ở từng chặng redirect,
không theo đăng nhập/tìm kiếm/lịch và không tải ảnh, video, file nén. Sitemap được
lấy từ robots.txt và thử `/sitemap.xml`. Script đọc cả sitemap index, sitemap gzip,
RSS và Atom khi các nguồn này được phát hiện/được thêm vào seeds.

Chạy thử một bài công khai với giới hạn nhỏ:

```powershell
python -m hust_collect --data-dir data/smoke crawl --seeds tests/smoke_seeds.txt --max-pages 3 --per-domain 3 --timeout 10
```

`--data-dir` là tùy chọn toàn cục, phải đặt **trước** tên lệnh.
Nhấn Ctrl+C để dừng; URL đã xử lý và các bản tải xong được lưu để dùng tiếp.
Không chạy nhiều tiến trình `crawl`/`reprocess` đồng thời trên cùng thư mục dữ liệu.

## 4. Bóc tách tài liệu bằng Tika

HTML được bóc tách trực tiếp bằng lxml. Nếu không cấu hình Tika, PDF dùng pypdf,
TXT đọc trực tiếp; Office được tải và giữ ở trạng thái `parse_error` để xử lý sau.
Để xử lý tài liệu bằng Tika, khởi động Tika Server đã cài và truyền địa chỉ:

```powershell
python -m hust_collect crawl --include-discovered --tika-url http://127.0.0.1:9998
python -m hust_collect reprocess --tika-url http://127.0.0.1:9998
```

Có thể khởi động Tika bằng JAR tải từ Apache:

```powershell
java -jar "duong-dan-den-tika-server-standard.jar" --host 127.0.0.1 --port 9998
```

Thay bằng tên JAR thực tế và dùng Java tương thích với phiên bản Tika đã chọn.
Script dùng endpoint `PUT /rmeta/text`, nhận nội dung và metadata JSON.
PDF scan cần OCR; bản này không tự cấu hình OCR hay tải mô hình OCR.
Lỗi trích xuất không xóa bản nội dung tốt trước đó.

## 5. Nội dung HTML và tiếng Việt

Script ưu tiên khối bài viết như `#bodytext`, `[itemprop=articleBody]`, `.article-body`,
sau đó thử `article`, `main` và heuristic dựa trên độ dài, đoạn văn, tỷ lệ liên kết.
Loại script, style, menu, header, footer, form và sidebar trong khối nội dung.
Trang danh mục vẫn được duyệt để tìm liên kết nhưng chỉ xuất khi vượt bộ lọc bài viết.
Heuristic có thể lấy thiếu nội dung hoặc nhận nhầm: cần kiểm tra mẫu từng miền.

Sau khi Inspect HTML, thêm XPath vào `extractors.json`, ví dụ:

```json
{
  "ten-mien-thuc-te.hust.edu.vn": {
    "title_xpath": "//h1",
    "content_xpath": "//*[@id='article-content']",
    "date_xpath": "//time/@datetime"
  }
}
```

Đây là ví dụ cấu hình, không phải selector đã xác nhận cho một miền cụ thể.
Sau khi sửa, chạy `reprocess` để bóc tách lại tệp đã tải mà không tải lại Web.
Ngày đăng không lấy được để `null`, không thay bằng ngày tải.

Mặc định tạo văn bản Unicode NFC, chuẩn hóa khoảng trắng, bản chữ thường và bản
không dấu. Không xóa dấu khỏi nội dung gốc, không tự bỏ từ dừng.
**Chuẩn hóa không đồng nghĩa tách từ**: `segmentation=not_applied` nếu chưa cấu hình.
Muốn tách từ, tự cài `py_vncorenlp`, Java và mô hình VnCoreNLP rồi chạy:

```powershell
python -m hust_collect reprocess --vncorenlp-dir "D:\models\vncorenlp" --tika-url http://127.0.0.1:9998
```

Mô hình phải có sẵn; script không tự tải. Áp dụng cùng cách xử lý cho truy vấn
khi tích hợp Lucene, bảo toàn các token nhiều âm tiết đã nối bằng dấu gạch dưới.

## 6. Chạy lại và lịch cập nhật

```powershell
python -m hust_collect crawl --include-discovered --revisit-hours 24
```

Mỗi lượt xử lý URL mới và URL đã tới lịch kiểm tra. Dùng ETag/Last-Modified nếu có;
HTTP 304 tái sử dụng bản tải cũ để khám phá liên kết và xử lý lại. Hash nội dung chính,
tiêu đề và ngày đăng xác định bài có thay đổi; hash tệp gốc lưu riêng.
Mỗi URL chỉ có một bản ghi hiện tại; đây không phải kho lịch sử mọi phiên bản.
404 được đánh dấu gỡ sau 3 lượt kiểm tra, 410 ngay lập tức; lỗi tạm thời thử lại
theo backoff, không xóa tài liệu. Bộ xuất chỉ ghi tài liệu còn active.

Dùng Windows Task Scheduler gọi Python của môi trường với các đối số trên,
đặt **Start in** là thư mục dự án. Mỗi lịch chạy có thể chỉ xử lý một phần hàng đợi
do giới hạn; tăng ngân sách theo kết quả quan sát, không đặt mục tiêu quét hết ngay.

## 7. Đầu ra và tích hợp Lucene

```text
data/
  crawl.sqlite3       # Miền, hàng đợi, trạng thái, bản ghi tài liệu hiện tại
  raw/<sha256>.bin     # Bytes gốc; tra MIME/URL tương ứng trong SQLite
  subdomains.txt      # Danh sách miền ở lần chạy discover
  documents.jsonl     # Bản xuất hiện tại, UTF-8, một tài liệu mỗi dòng
  all_extracted_results.json # Mảng JSON bốn trường theo file mẫu
```

```powershell
python -m hust_collect export
python -m hust_collect stats
```

`documents.jsonl` tự xuất sau crawl/reprocess; export ghi lại snapshot, không append.
Trường chính: `id`, `url`, `final_url`, `domain`, `source_url`, `title`, `content`,
`normalized_content`, `folded_content`, `segmented_content`, `segmentation`,
`published_at`, `fetched_at`, `first_seen_at`, `updated_at`, `file_type`,
`extraction_method`, `content_hash`, `duplicate_group`.
Timestamps hệ thống dùng UTC/ISO 8601; ngày đăng giữ định dạng nguồn.
`source_url` là nguồn khám phá đầu tiên, có thể là URL bài chứa tệp, sitemap hoặc nhãn seed.
`duplicate_group` là hash nội dung chuẩn hóa: nhận biết trùng tuyệt đối, chưa có MinHash
cho gần trùng. Giữ các URL nguồn để truy vết, không tự gộp chúng.
Khi nạp snapshot vào Lucene: update theo `id`; xóa các ID không còn trong snapshot
hoặc xây lại chỉ mục. Chỉ update các dòng đang có sẽ không loại được tài liệu đã gỡ.

## 8. Kiểm tra

```powershell
python -m unittest discover -s tests -v
```

Tests chạy offline bằng phản hồi HTTP giả lập: phạm vi URL/redirect, robots.txt,
miền mới và sitemap, Unicode, lọc danh mục, HTTP 304, cập nhật không trùng bản ghi,
giữ tệp khi bóc tách lỗi, chạy tiếp và xử lý tài liệu gỡ.

Giới hạn: không render JavaScript, không truy cập đăng nhập, không bảo đảm tìm hết
miền con hoặc thu thập đủ mọi bài. Chưa gồm dịch vụ Lucene/giao diện tìm kiếm.

Tài liệu tham khảo:
- https://docs.python.org/3/library/urllib.robotparser.html
- https://lxml.de/parsing.html
- https://cwiki.apache.org/confluence/spaces/TIKA/pages/148639291/TikaServer
- https://docs.projectdiscovery.io/opensource/subfinder/usage
"# IT4863---BTL-" 
