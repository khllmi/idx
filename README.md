# Screener Saham IDX

Dashboard screening saham Bursa Efek Indonesia, data dari Yahoo Finance,
di-publish sebagai situs statis di GitHub Pages.

## Cara kerjanya

```
lokal / GitHub Actions                      GitHub Pages (statis)
  idx_data.py → idx.db → export_web.py → docs/data.json → docs/index.html
   (scraping)             (ekspor)           (commit)        (browser)
```

GitHub Pages **cuma melayani file statis** — tidak bisa menjalankan Python. Jadi
scraping dikerjakan di luar, hasilnya dibekukan jadi `data.json`, dan yang
di-publish hanya JSON + HTML. Halaman itu murni client-side: filter, sorting dan
grafiknya jalan di browser pengunjung, tanpa server.

| File | Fungsi |
|---|---|
| `idx_data.py` | ambil data Yahoo → SQLite (`idx.db`) |
| `screener.py` | hitung RSI, MA, momentum, likuiditas, skor |
| `export_web.py` | `idx.db` → `docs/data.json` (lengkap, harian) |
| `refresh_quotes.py` | harga terbaru → `docs/quotes.json` (ringan, tiap 15 menit) |
| `docs/index.html` | dashboard statis (tanpa dependensi, tanpa build) |
| `app.py` | versi Streamlit, untuk dipakai lokal saja |
| `.github/workflows/update-data.yml` | scrape lengkap tiap hari kerja 17:00 WIB |
| `.github/workflows/refresh-quotes.yml` | refresh harga tiap 15 menit saat bursa buka |

## Seberapa "realtime" ini?

**Tidak realtime, dan tidak bisa.** Angka di situs tertinggal **20-40 menit** dari
pasar. Itu batas atas tanpa server sendiri dan tanpa feed IDX berbayar. Tiga
sumber keterlambatan menumpuk:

| Penyebab | Delay |
|---|---|
| Data Yahoo untuk IDX memang delay | ~15-20 menit |
| Cron tiap 15 menit | 0-15 menit |
| Runner GitHub sering telat saat sibuk | 5-20 menit |

Kenapa tidak fetch Yahoo langsung dari browser pengunjung? Yahoo tidak mengirim
header CORS, jadi browser akan memblokir permintaannya. Memakai proxy CORS publik
bisa, tapi rapuh dan melanggar ToS Yahoo. Realtime sungguhan butuh server yang
memegang websocket ke feed berlisensi — di luar jangkauan GitHub Pages.

**Jangan pakai angka ini untuk keputusan intraday.** Untuk screening mingguan atau
bulanan, delay 30 menit tidak berarti apa-apa.

Dua jalur dipisah karena biayanya beda jauh. `idx_data.py prices` menarik riwayat
3 tahun per emiten (15-25 menit) — itu jalan sekali sehari. `refresh_quotes.py`
memanggil `yf.screen()` yang mengembalikan harga seluruh bursa dalam ~4 request
(beberapa detik) — itu yang jalan tiap 15 menit. Halamannya poll `quotes.json`
tiap 60 detik dan berhenti saat tab tidak dilihat.

Pil status di header menunjukkan bursa buka atau tutup dan umur harga; kalau data
lewat satu jam, pilnya berubah kuning.

Mau lebih jarang? Ubah cron di `refresh-quotes.yml` jadi `*/30`. Perlu diingat
workflow terjadwal **otomatis dimatikan GitHub setelah repo 60 hari tanpa
aktivitas** — kalau situs tiba-tiba berhenti update, cek dulu tab Actions.

## Jalankan lokal

```bash
pip install -r requirements.txt

python idx_data.py universe              # ~700-900 emiten bursa JKT
python idx_data.py prices --limit 50     # tes dulu 50 emiten
python idx_data.py fundamentals --limit 50
python export_web.py
python refresh_quotes.py                 # harga terbaru (opsional, cepat)

cd docs && python -m http.server 8000    # buka http://localhost:8000
```

**Jangan buka `docs/index.html` lewat double-click.** Lewat `file://`, browser
memblokir `fetch("data.json")` karena aturan CORS, dan halamannya diam-diam jatuh
ke data contoh. Selalu lewat HTTP server, sependek apa pun.

Kalau 50 emiten sudah lancar, jalankan penuh: `python idx_data.py prices`
(~15-25 menit) lalu `python idx_data.py fundamentals --top 300`.

## Publish ke GitHub Pages

1. Push repo ini ke GitHub.
2. **Settings → Pages → Source: Deploy from a branch**, pilih branch `main`
   dan folder **`/docs`**. Simpan.
3. Tunggu 1-2 menit. Situsmu hidup di
   `https://<username>.github.io/<nama-repo>/`

Mau update otomatis? Workflow-nya sudah ada. Aktifkan dengan:
**Settings → Actions → General → Workflow permissions → Read and write permissions.**
Tanpa itu, langkah `git push` di workflow akan gagal dengan 403.

Repo publik dapat GitHub Actions gratis; repo privat punya kuota menit bulanan.

## Dua hal yang gampang bikin salah

### 1. Arah ranking pada skor komposit

Bug ini sempat ada dan baru ketahuan waktu diuji:

```python
s.rank(pct=True, ascending=True)   # nilai KECIL dapat persentil RENDAH
```

Untuk ROE itu benar (makin besar makin bagus). Untuk PER, PBV dan DER itu
**kebalik** — saham termurah justru dapat skor nol, dan screener merekomendasikan
yang termahal. Makanya `pct_rank()` sekarang pakai parameter `higher_is_better`,
bukan `ascending`, supaya niatnya kebaca dari pemanggilnya.

Tesnya gampang: bikin tiga baris murah/tengah/mahal, pastikan yang murah menang.

### 2. Satuan Yahoo yang menyesatkan

- `returnOnEquity` = `0.18`, bukan `18`
- `debtToEquity` = `120`, artinya `1.2x`
- `dividendYield` **berubah format antar versi** (0.045 vs 4.5) — kode ini
  mendeteksi lewat median seluruh kolom, bukan menebak per baris

## Gotcha khas pasar Indonesia

- **Likuiditas itu filter nomor satu.** Dari ~800 emiten, mungkin cuma 200-an
  yang nilai transaksinya > Rp 1 miliar/hari. Saham PER 3 dan PBV 0,3 tidak ada
  gunanya kalau tidak bisa dijual.
- **Emiten tanpa data ROE lolos gratis di dimensi kualitas.** Skornya
  dinormalisasi ke bobot yang terpakai, jadi mereka cuma dinilai dari PER, PBV
  dan momentum. Di tabel mereka ditandai titik kuning; centang "Punya data
  ROE/DER" kalau mau adil.
- **Auto-rejection ±20-35%** bikin volatilitas kelihatan lebih rendah dari
  kenyataan, dan bikin gap besar saat ARA/ARB beruntun.
- **Emiten gorengan** lolos screening value dengan gampang: PBV rendah karena
  ekuitasnya digelembungkan lewat revaluasi aset. Cek ulang ROE, arus kas
  operasi, dan siapa pemegang sahamnya.
- **Bank tidak bisa dinilai pakai DER** — leverage memang sifat bisnisnya. Pakai
  PBV + ROE + NPL.
- **Laporan kuartalan telat.** Q3 baru keluar akhir Oktober-November, dan Yahoo
  lebih telat lagi.

## Roadmap

1. **Incremental update** — `prices` sekarang selalu `replace`. Ubah jadi
   `INSERT OR IGNORE` dengan primary key `(ticker, date)` supaya update harian
   cuma tarik data baru. Ini juga memangkas waktu workflow drastis.
2. **Backtest** — sebelum percaya `score`, uji: kalau tiap bulan beli top-20
   skor, hasilnya bagaimana vs IHSG (`^JKSE`)?
3. **Halaman detail per emiten** — chart harga + MA, dari `prices` yang
   diekspor terpisah per ticker.
4. **Fundamental dari XBRL IDX** — kalau akurasi Yahoo mulai mengganggu.
5. **Foreign flow & bandarmology** — data broker summary per saham.

---

Untuk riset pribadi, bukan rekomendasi beli atau jual.
