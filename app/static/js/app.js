(function () {
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content;

  function toast(msg) {
    const el = document.getElementById('appToast');
    document.getElementById('appToastBody').textContent = msg;
    bootstrap.Toast.getOrCreateInstance(el, { delay: 2500 }).show();
  }
  window.appToast = toast;

  // ปุ่มคัดลอกข้อมูล: ดึงข้อมูลชุดเดียวกับไฟล์ส่งออก แล้วใส่คลิปบอร์ด (หัวข้อ 8)
  document.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-copy-url]');
    if (!btn) return;
    e.preventDefault();
    const url = btn.dataset.copyUrl;
    let rows = 0;
    const fetchText = fetch(url, { credentials: 'same-origin' }).then(async (r) => {
      if (!r.ok) throw new Error(r.status);
      rows = parseInt(r.headers.get('X-Row-Count') || '0', 10);
      return r.text();
    });
    try {
      if (window.ClipboardItem && navigator.clipboard?.write) {
        // Safari ต้องสร้าง ClipboardItem ภายใน user gesture
        const item = new ClipboardItem({ 'text/plain': fetchText.then((t) => new Blob([t], { type: 'text/plain' })) });
        await navigator.clipboard.write([item]);
      } else {
        const text = await fetchText;
        await navigator.clipboard.writeText(text);
      }
      await fetchText;
      toast(`คัดลอกแล้ว ${rows} แถว`);
    } catch (err) {
      try {
        const text = await fetchText;
        const ta = document.createElement('textarea');
        ta.value = text; document.body.appendChild(ta); ta.select();
        document.execCommand('copy'); ta.remove();
        toast(`คัดลอกแล้ว ${rows} แถว`);
      } catch (e2) {
        toast('คัดลอกไม่สำเร็จ (ต้องใช้ HTTPS และมีสิทธิ์เข้าถึงข้อมูล)');
      }
    }
  });

  // ยืนยันก่อนส่งฟอร์มที่สำคัญ
  document.addEventListener('submit', (e) => {
    const f = e.target;
    const msg = f.dataset.confirm || e.submitter?.dataset.confirm;
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // ตรวจรหัสนักศึกษาสด (หน้า ลงทะเบียน)
  const code = document.getElementById('student_code');
  if (code) {
    const out = document.getElementById('code_info');
    const check = async () => {
      const v = code.value.trim();
      if (!/^\d{9}$/.test(v)) { out.textContent = 'รหัสนักศึกษา 9 หลัก'; out.className = 'form-text'; return; }
      const r = await fetch(`/api/parse-code?code=${v}`);
      const d = await r.json();
      if (!d.ok) { out.textContent = 'รูปแบบไม่ถูกต้อง'; return; }
      const type = { SCI: 'นักศึกษาคณะวิทยาศาสตร์ฯ', OTHER_MRU: 'นักศึกษา มรย. ต่างคณะ' }[d.account_type];
      out.className = 'form-text text-success';
      out.textContent = `${type}${d.program ? ' · ' + d.program : ''}${d.year_level ? ' · ชั้นปี ' + d.year_level : ''} · ปีเข้า ${d.admit_year}${d.needs_review ? ' (รอผู้ดูแลตรวจสาขา)' : ''}`;
    };
    code.addEventListener('input', check);
    check();
  }

  // ย่อรูปที่เบราว์เซอร์ก่อนอัปโหลด และตรวจขนาด/จำนวนไฟล์
  const maxMB = parseFloat(document.querySelector('meta[name="max-file-mb"]')?.content || '2');
  const MAX_SIDE = 1600;

  function resizeImage(file) {
    return new Promise((resolve) => {
      if (!/^image\/(jpeg|png|webp)$/.test(file.type)) return resolve(file);
      const img = new Image();
      const url = URL.createObjectURL(file);
      img.onload = () => {
        URL.revokeObjectURL(url);
        const scale = Math.min(1, MAX_SIDE / Math.max(img.width, img.height));
        if (scale === 1 && file.size < 400 * 1024) return resolve(file);
        const canvas = document.createElement('canvas');
        canvas.width = Math.round(img.width * scale);
        canvas.height = Math.round(img.height * scale);
        const ctx = canvas.getContext('2d');
        ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        canvas.toBlob((blob) => {
          if (!blob || blob.size >= file.size) return resolve(file);
          const name = file.name.replace(/\.[^.]+$/, '') + '.jpg';
          resolve(new File([blob], name, { type: 'image/jpeg' }));
        }, 'image/jpeg', 0.82);
      };
      img.onerror = () => { URL.revokeObjectURL(url); resolve(file); };
      img.src = url;
    });
  }

  document.addEventListener('change', async (e) => {
    const input = e.target;
    if (!(input instanceof HTMLInputElement) || input.type !== 'file' || !input.files.length) return;
    const maxFiles = parseInt(input.dataset.maxFiles || '0', 10);
    if (maxFiles && input.files.length > maxFiles) {
      alert(`เลือกได้ไม่เกิน ${maxFiles} ไฟล์`);
      input.value = '';
      return;
    }
    const dt = new DataTransfer();
    for (const f of input.files) {
      const out = await resizeImage(f);
      if (out.size > maxMB * 1024 * 1024) {
        alert(`ไฟล์ ${f.name} ใหญ่เกิน ${maxMB} MB`);
        input.value = '';
        input.dispatchEvent(new Event('change', { bubbles: true }));
        return;
      }
      dt.items.add(out);
    }
    input.files = dt.files;
  });

  // ---------- แถบบนสุด: ปรับขนาดตัวอักษร + เปลี่ยนภาษา (จำค่าไว้ใน localStorage) ----------
  const root = document.documentElement;
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* โหมดส่วนตัว */ } },
  };

  function applyFontSize(fs) {
    if (fs === 'm') delete root.dataset.fs; else root.dataset.fs = fs;
    document.querySelectorAll('button[data-fs]').forEach((b) => b.classList.toggle('active', b.dataset.fs === fs));
  }

  // คำแปลอังกฤษของข้อความโครงหน้าเว็บ (ข้อความไทยอยู่ใน HTML อยู่แล้ว) เนื้อหาจากฐานข้อมูลไม่ถูกแปล
  const EN = {
    'brand': 'Science Student Club',
    'nav.home': 'Home', 'nav.equipment': 'Equipment', 'nav.org': 'Organization', 'nav.help': 'Help', 'nav.login': 'Sign in',
    'svc.register': 'Register', 'svc.shop': 'Shop', 'svc.borrow': 'Borrow & Return', 'svc.order': 'Order',
    'svc.faq': 'FAQ', 'svc.recruit': 'Committee Recruitment', 'svc.activity': 'Activities', 'svc.team': 'Join Staff Team', 'svc.ticket': 'Requests',
    'svc.rooms': 'Room Booking',
    'home.news': 'News & Announcements', 'home.all': 'View all', 'home.latest': 'Latest', 'home.notice': 'Announcements',
    'home.services': 'Services', 'home.open': 'Now Open for Applications',
    'foot.address': 'Yala Rajabhat University, 133 Thetsaban 3 Road, Sateng, Mueang, Yala 95000',
    'foot.about': 'About the System', 'foot.about1': 'About STA-SCMS', 'foot.about2': 'User Guide', 'foot.about3': 'Privacy Policy',
    'foot.uni': 'University Services', 'foot.uni1': 'University Website', 'foot.uni2': 'Education Service System',
    'foot.uni3': 'Academic Resources Office', 'foot.uni4': 'Student Development Division',
    'foot.club': 'Club Services', 'foot.faculty': 'Faculty',
    'foot.copy': 'Student Club Management System, Faculty of Science Technology and Agriculture, Yala Rajabhat University',
    'soon.text': 'This service is being prepared and will be available soon.', 'soon.back': 'Back to Home',
  };

  function applyLang(lang) {
    root.lang = lang;
    document.querySelectorAll('[data-i18n]').forEach((el) => {
      if (el.dataset.th === undefined) el.dataset.th = el.textContent;
      el.textContent = lang === 'en' && EN[el.dataset.i18n] ? EN[el.dataset.i18n] : el.dataset.th;
    });
    document.querySelectorAll('button[data-lang]').forEach((b) => b.classList.toggle('active', b.dataset.lang === lang));
  }

  applyFontSize(store.get('sta-fs') || 'm');
  applyLang(store.get('sta-lang') === 'en' ? 'en' : 'th');

  document.addEventListener('click', (e) => {
    const fsBtn = e.target.closest('button[data-fs]');
    if (fsBtn) { store.set('sta-fs', fsBtn.dataset.fs); applyFontSize(fsBtn.dataset.fs); return; }
    const langBtn = e.target.closest('button[data-lang]');
    if (langBtn) { store.set('sta-lang', langBtn.dataset.lang); applyLang(langBtn.dataset.lang); }
  });

  window.postJSON =(url, data) => fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf }, body: JSON.stringify(data),
  });
})();
