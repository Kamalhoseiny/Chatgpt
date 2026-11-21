# -*- coding: utf-8 -*-
"""Core SQLite MKS merge engine adapted from V9; no desktop GUI imports."""
import os, re, sqlite3, shutil, csv, hashlib, tempfile
from pathlib import Path
from datetime import date

def norm(v): return '' if v is None else str(v).strip()
def num(v):
    try: return float(v or 0)
    except: return 0.0

def connect(p):
    c=sqlite3.connect(p); c.execute('PRAGMA foreign_keys=OFF'); return c

def table_exists(c,t):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(t,)).fetchone() is not None

def cols(c,t): return [r[1] for r in c.execute(f'PRAGMA table_info("{t}")')]
def allrows(c,t): return c.execute(f'SELECT * FROM "{t}"').fetchall()
def dictrows(c,t):
    cs=cols(c,t); return [dict(zip(cs,r)) for r in allrows(c,t)]
def max_id(c,t):
    try: return int(c.execute(f'SELECT COALESCE(MAX(_id),0) FROM "{t}"').fetchone()[0])
    except: return 0

def insert_dict(c,t,d):
    cs=list(d.keys()); c.execute(f'INSERT INTO "{t}" ({",".join(chr(34)+x+chr(34) for x in cs)}) VALUES ({",".join("?" for _ in cs)})',[d[x] for x in cs])

def update_dict(c,t,d):
    cs=[x for x in d if x!='_id']; c.execute(f'UPDATE "{t}" SET {",".join(chr(34)+x+chr(34)+"=?" for x in cs)} WHERE _id=?',[d[x] for x in cs]+[d['_id']])

def rowhash(d):
    return hashlib.sha256(repr(tuple((k,d.get(k)) for k in d)).encode('utf-8','replace')).hexdigest()

def ts_key(d):
    s=norm(d.get('docDate') or d.get('inDate')); tm=''.join(ch for ch in norm(d.get('inTime')) if ch.isdigit())
    return (s,(tm+'000000')[:6])

def comparable_payment_value(k,v):

    if k == 'docDesc':
        return re.sub(r'\s*\[منبع فایل:.*?\]\s*', ' ', norm(v)).strip()
    return v


def has_document_identity_collision(a,b):
    """True when equal database IDs clearly refer to different accounting documents."""
    # Person and document kind are strong identity fields; changes here are not treated
    # as an ordinary correction to the same document.
    if norm(a.get('personId')) != norm(b.get('personId')):
        return True
    if norm(a.get('docKind')) != norm(b.get('docKind')):
        return True

    def meaningful_number(v):
        s=norm(v)
        return '' if s in ('', '0') else s

    an=meaningful_number(a.get('docNum')); bn=meaningful_number(b.get('docNum'))
    if an and bn and an != bn:
        return True
    if bool(an) != bool(bn):
        return True

    # If there is no useful document number, use Jalali date and saved time to avoid
    # merging independent same-customer transactions that happen to share a local _id.
    if not an and not bn:
        ad=norm(a.get('docDate') or a.get('inDate'))
        bd=norm(b.get('docDate') or b.get('inDate'))
        if ad != bd:
            return True
        # System-generated rows without a customer (for example, cash-fund setup)
        # can receive a different creation time in each database; do not split them.
        if norm(a.get('personId')):
            at=''.join(ch for ch in norm(a.get('inTime')) if ch.isdigit())
            bt=''.join(ch for ch in norm(b.get('inTime')) if ch.isdigit())
            if at != bt:
                return True
    return False

def same_payment(a,b):
    keys=(set(a)|set(b)) - {'_id'}
    return all(comparable_payment_value(k,a.get(k))==comparable_payment_value(k,b.get(k)) for k in keys)

def add_source_tag(d, source_tag):
    if 'docDesc' in d and source_tag not in norm(d.get('docDesc')):
        d['docDesc']=(norm(d.get('docDesc'))+' '+source_tag).strip()
    return d

def gregorian_to_jalali(gy,gm,gd):
    gdm=[0,31,59,90,120,151,181,212,243,273,304,334]
    if gy>1600: jy=979; gy-=1600
    else: jy=0; gy-=621
    gy2=gy+1 if gm>2 else gy
    days=365*gy+(gy2+3)//4-(gy2+99)//100+(gy2+399)//400-80+gd+gdm[gm-1]
    jy += 33*(days//12053); days%=12053
    jy += 4*(days//1461); days%=1461
    if days>365: jy+=(days-1)//365; days=(days-1)%365
    jm=days//31+1 if days<186 else (days-186)//30+7
    jd=days%31+1 if days<186 else (days-186)%30+1
    return jy,jm,jd

def parse_jdate(s):
    m=re.search(r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})',norm(s))
    if not m: return None
    try:
        y,mo,d=map(int,m.groups())
        if not (1200<=y<=1600 and 1<=mo<=12 and 1<=d<=31): return None
        return y,mo,d
    except: return None

def today_jalali(): return gregorian_to_jalali(*date.today().timetuple()[:3])
def jalali_months_since(jd, today=None):
    if not jd: return None
    ty,tm,td=today or today_jalali(); y,m,d=jd
    n=(ty*12+tm)-(y*12+m)
    if td<d: n-=1
    return max(0,n)

def jdate_key(s):
    j=parse_jdate(s); return j or (0,0,0)

def copy_base(src,out):
    if os.path.exists(out): os.remove(out)
    shutil.copy2(src,out)

def person_maps(client_rows, base_rows):

    base_by_id={int(d['_id']):dict(d) for d in base_rows}
    by_identity={}
    for bid,d in base_by_id.items():
        name=norm(d.get('fullname')).casefold()
        phone=norm(d.get('mobilephone'))
        if name:
            by_identity.setdefault((name,phone),[]).append(bid)
    next_id=max(base_by_id.keys() or [0])+1
    mp={}; news=[]
    for d0 in client_rows:
        d=dict(d0)
        old=int(d['_id'])
        name=norm(d.get('fullname')).casefold()
        phone=norm(d.get('mobilephone'))
        existing=base_by_id.get(old)

        if existing is not None:
            ename=norm(existing.get('fullname')).casefold()
            ephone=norm(existing.get('mobilephone'))
            same_identity = (name and name == ename) or (phone and ephone and phone == ephone)
            if same_identity:
                mp[old]=old
                continue

            matches=by_identity.get((name,phone),[]) if name else []
            if len(matches)==1:
                mp[old]=matches[0]
                continue
            while next_id in base_by_id:
                next_id+=1
            new_id=next_id; next_id+=1
            d['_id']=new_id
            mp[old]=new_id
            news.append(d)
            base_by_id[new_id]=d
            if name:
                by_identity.setdefault((name,phone),[]).append(new_id)
            continue

        matches=by_identity.get((name,phone),[]) if name else []
        if len(matches)==1:
            mp[old]=matches[0]
        else:

            target=old
            if target in base_by_id:
                while next_id in base_by_id:
                    next_id+=1
                target=next_id; next_id+=1
            d['_id']=target
            mp[old]=target
            news.append(d)
            base_by_id[target]=d
            if name:
                by_identity.setdefault((name,phone),[]).append(target)
    return mp,news

def inventory_maps(client_rows,base_rows):
    base_by_id={int(d['_id']):d for d in base_rows}; by_name={}
    for bid,d in base_by_id.items(): by_name.setdefault(norm(d.get('articleName')).casefold(),[]).append(bid)
    nextid=max([int(x['_id']) for x in base_rows] or [0])+1; mp={}; news=[]
    for d in client_rows:
        old=int(d['_id'])
        if old in base_by_id: mp[old]=old; continue
        key=norm(d.get('articleName')).casefold(); cand=by_name.get(key,[])
        if len(cand)==1 and key: mp[old]=cand[0]
        else:
            nd=dict(d); nd['_id']=nextid; nextid+=1; mp[old]=nd['_id']; news.append(nd); base_by_id[nd['_id']]=nd
    return mp,news

def child_rows(c,pid):
    if not table_exists(c,INVTRAN): return []
    cs=cols(c,INVTRAN); return [dict(zip(cs,r)) for r in c.execute('SELECT * FROM invenTran WHERE docId=?',(pid,)).fetchall()]

def insert_children(base,source,src_pid,target_pid,invmap):
    if not table_exists(base,INVTRAN): return
    base.execute('DELETE FROM invenTran WHERE docId=?',(target_pid,))
    nextid=max_id(base,INVTRAN)+1
    for d in child_rows(source,src_pid):
        d=dict(d); d['_id']=nextid; nextid+=1
        if d.get('articleId') in invmap: d['articleId']=invmap[d['articleId']]
        d['docId']=target_pid; insert_dict(base,INVTRAN,d)

def _merge_three_impl(files,out,log=None):

    if len(files)!=3: raise ValueError('دقیقاً 3 فایل MKS لازم است.')
    if len({os.path.abspath(x) for x in files})<3: raise ValueError('سه فایل ورودی باید متفاوت باشند.')
    if os.path.abspath(out) in [os.path.abspath(x) for x in files]: raise ValueError('خروجی نباید یکی از ورودی‌ها باشد.')

    def file_latest(path):
        cc=connect(path)
        try:
            if not table_exists(cc,PAYMENTS): return ('0000/00/00','000000')
            return max((ts_key(x) for x in dictrows(cc,PAYMENTS)), default=('0000/00/00','000000'))
        finally: cc.close()

    ordered=sorted(files,key=file_latest)
    newest_i=len(ordered)-1
    if log: log('ترتیب بر اساس آخرین تاریخ/ساعت سند: '+'  ←  '.join(os.path.basename(x) for x in ordered))

    copy_base(ordered[newest_i],out)
    base=connect(out)
    sources=[connect(x) for x in ordered]
    audit=[]; conflicts=[]
    person_maps_by_src={}; inventory_maps_by_src={}; payment_maps_by_src={}
    origin_payment={int(d['_id']):newest_i for d in dictrows(base,PAYMENTS)}

    try:

        pbase=dictrows(base,PERSONS)
        for ci,src in enumerate(sources):
            pmap,pnew=person_maps(dictrows(src,PERSONS),pbase)
            person_maps_by_src[ci]=pmap
            if ci != newest_i:
                existing={int(d['_id']) for d in pbase}
                for d in pnew:
                    if int(d['_id']) not in existing:
                        insert_dict(base,PERSONS,d); pbase.append(d); existing.add(int(d['_id']))

        pbase=dictrows(base,PERSONS)

        ibase=dictrows(base,INVENTORY)
        for ci,src in enumerate(sources):
            imap,inew=inventory_maps(dictrows(src,INVENTORY),ibase)
            inventory_maps_by_src[ci]=imap
            if ci != newest_i:
                existing={int(d['_id']) for d in ibase}
                for d in inew:
                    if int(d['_id']) not in existing:
                        insert_dict(base,INVENTORY,d); ibase.append(d); existing.add(int(d['_id']))
            ibase=dictrows(base,INVENTORY)

        base_pay={int(d['_id']):d for d in dictrows(base,PAYMENTS)}
        newest_tag=f'[منبع فایل: {os.path.basename(ordered[newest_i])}]'
        for pid,d in list(base_pay.items()):
            if 'docDesc' in d:
                add_source_tag(d,newest_tag); update_dict(base,PAYMENTS,d); base_pay[pid]=d

        used_ids=set(base_pay)
        # Allocate collision IDs above every source ID, not just the base maximum;
        # otherwise a new ID could collide with a later source row during this merge.
        max_source_id=max((int(x['_id']) for ss in sources for x in dictrows(ss,PAYMENTS)
                           if x.get('_id') is not None), default=0)
        nextid=max(max(used_ids or {0}),max_source_id)+1
        for ci,src in enumerate(sources):
            pm=person_maps_by_src[ci]
            im=inventory_maps_by_src[ci]
            source_tag=f'[منبع فایل: {os.path.basename(ordered[ci])}]'
            this_payment_map={}
            src_payments=dictrows(src,PAYMENTS)
            for d0 in src_payments:
                d=dict(d0); old=int(d['_id'])
                d['personId']=pm.get(d.get('personId'),d.get('personId'))
                if old in base_pay:
                    b=base_pay[old]
                    if has_document_identity_collision(b,d):
                        # A previous source may already have contributed this exact document
                        # under a remapped ID. Match it before creating another copy.
                        exact_collision_copy=next((bid for bid,existing_doc in base_pay.items()
                                                    if bid != old and same_payment(existing_doc,d)),None)
                        if exact_collision_copy is not None:
                            this_payment_map[old]=exact_collision_copy
                            existing_doc=base_pay[exact_collision_copy]
                            add_source_tag(existing_doc,source_tag)
                            update_dict(base,PAYMENTS,existing_doc)
                            audit.append([ci,'SKIP_IDENTICAL_REMAP',old,exact_collision_copy,
                                          norm(d.get('docNum')),
                                          'سند تکراری قبلاً با شناسه خروجی جدید ثبت شده بود'])
                            continue
                        # The same local _id is reused for a different document in another
                        # offline database. Keep both documents under distinct output IDs.
                        target=nextid
                        while target in used_ids: target+=1
                        nextid=target+1
                        used_ids.add(target)
                        this_payment_map[old]=target
                        d['_id']=target; add_source_tag(d,source_tag)
                        insert_dict(base,PAYMENTS,d); base_pay[target]=d; origin_payment[target]=ci
                        insert_children(base,src,old,target,im)
                        audit.append([ci,'ID_COLLISION_INSERT',old,target,norm(d.get('docNum')),
                                      'شناسه دیتابیس مشترک بود اما هویت سند متفاوت بود؛ هر دو سند حفظ شدند'])
                        conflicts.append([ci,old,norm(d.get('docNum')),
                                          'شناسه محلی مشترک با سندی متفاوت؛ سند جداگانه حفظ شد'])
                        continue
                    this_payment_map[old]=old
                    if same_payment(b,d):
                        add_source_tag(b,source_tag)
                        update_dict(base,PAYMENTS,b); base_pay[old]=b
                        audit.append([ci,'SKIP_IDENTICAL',old,old,norm(d.get('docNum')),''])
                        continue
                    conflicts.append([ci,old,norm(d.get('docNum')),
                        'نسخه‌ها متفاوت‌اند؛ نسخه فایل جدیدتر حفظ شد' if origin_payment.get(old,newest_i)>=ci else 'نسخه جدیدترِ فایل‌های قدیمی‌تر جایگزین شد'])
                    if origin_payment.get(old,newest_i) < ci:
                        d['_id']=old; add_source_tag(d,source_tag)
                        update_dict(base,PAYMENTS,d); base_pay[old]=d; origin_payment[old]=ci
                        insert_children(base,src,old,old,im)
                        audit.append([ci,'UPDATE_LATER_SNAPSHOT',old,old,norm(d.get('docNum')),''])
                    else:
                        add_source_tag(b,source_tag); update_dict(base,PAYMENTS,b); base_pay[old]=b
                        audit.append([ci,'KEEP_NEWEST_SNAPSHOT',old,old,norm(d.get('docNum')),''])
                    continue

                exact=None
                for bid,b in base_pay.items():
                    if same_payment(b,d):
                        exact=bid; break
                if exact is not None:
                    this_payment_map[old]=exact
                    b=base_pay[exact]; add_source_tag(b,source_tag)
                    update_dict(base,PAYMENTS,b); base_pay[exact]=b
                    audit.append([ci,'SKIP_IDENTICAL_NEW',old,exact,norm(d.get('docNum')),''])
                    continue

                target=old
                if target in used_ids:
                    while nextid in used_ids: nextid+=1
                    target=nextid; nextid+=1
                used_ids.add(target); this_payment_map[old]=target
                d['_id']=target; add_source_tag(d,source_tag)
                insert_dict(base,PAYMENTS,d); base_pay[target]=d; origin_payment[target]=ci
                insert_children(base,src,old,target,im)
                audit.append([ci,'INSERT_OLDER_ONLY',old,target,norm(d.get('docNum')),''])
            payment_maps_by_src[ci]=this_payment_map

        if table_exists(base,'invoiceNums'):
            for row in dictrows(base,'invoiceNums'):
                rid=row.get('_id')
                for src in sources:
                    if not table_exists(src,'invoiceNums'): continue
                    other=src.execute('SELECT * FROM invoiceNums WHERE _id=?',(rid,)).fetchone()
                    if not other: continue
                    names=cols(src,'invoiceNums'); od=dict(zip(names,other))
                    changes={k:max(int(num(row.get(k))),int(num(od.get(k)))) for k in ('sell','buy','retSell','retBuy','preSell') if k in row and k in od}
                    if changes:
                        for k,v in changes.items(): row[k]=v
                update_dict(base,'invoiceNums',row)

        skip={'android_metadata',PERSONS,PAYMENTS,INVTRAN,INVENTORY,'banks','invoiceNums','paymentsTemp','invenTranTemp'}
        table_names=[r[0] for r in base.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for table in table_names:
            if table in skip: continue
            if not cols(base,table): continue
            base_cols=cols(base,table)
            if '_id' not in base_cols: continue
            origins={int(r[0]):newest_i for r in base.execute(f'SELECT _id FROM "{table}"').fetchall()}
            for ci,src in enumerate(sources):
                if ci==newest_i or not table_exists(src,table): continue
                src_cols=cols(src,table)
                if src_cols != base_cols: continue
                for raw in allrows(src,table):
                    d=dict(zip(src_cols,raw))
                    if 'personId' in d: d['personId']=person_maps_by_src[ci].get(d.get('personId'),d.get('personId'))
                    if table in ('bankTranAddInfo',) and 'docId' in d:
                        d['docId']=person_maps_by_src[ci].get(d.get('docId'),d.get('docId'))
                    if table in ('chequeStatus','billAddInfo') and 'docId' in d:
                        d['docId']=payment_maps_by_src.get(ci,{}).get(d.get('docId'),d.get('docId'))
                    if table=='TransferDocDetail' and 'sourceTransId' in d:
                        d['sourceTransId']=payment_maps_by_src.get(ci,{}).get(d.get('sourceTransId'),d.get('sourceTransId'))
                    if 'articleId' in d: d['articleId']=inventory_maps_by_src[ci].get(d.get('articleId'),d.get('articleId'))
                    rid=int(d['_id']) if d.get('_id') is not None else None
                    if rid is not None and base.execute(f'SELECT 1 FROM "{table}" WHERE _id=?',(rid,)).fetchone():
                        oldrow=dict(zip(base_cols,base.execute(f'SELECT * FROM "{table}" WHERE _id=?',(rid,)).fetchone()))
                        if rowhash(oldrow)==rowhash(d): continue
                        if origins.get(rid,newest_i) < ci:
                            try:
                                update_dict(base,table,d); origins[rid]=ci
                            except sqlite3.IntegrityError:
                                audit.append([ci,'AUX_CONFLICT_KEEP_EXISTING',rid,rid,table,'تعارض قید یکتا؛ رکورد موجود حفظ شد'])
                        continue
                    try:
                        insert_dict(base,table,d); origins[rid]=ci if rid is not None else ci
                    except sqlite3.IntegrityError:
                        audit.append([ci,'AUX_INSERT_SKIPPED',rid or '',rid or '',table,'تعارض قید یکتا؛ رکورد افزوده نشد'])

        orphan_p=base.execute('SELECT COUNT(*) FROM payments p LEFT JOIN persons x ON x._id=p.personId WHERE p.personId IS NOT NULL AND x._id IS NULL').fetchone()[0]
        orphan_l=base.execute('SELECT COUNT(*) FROM invenTran t LEFT JOIN inventory i ON i._id=t.articleId WHERE t.articleId IS NOT NULL AND i._id IS NULL').fetchone()[0]
        if orphan_p or orphan_l: raise RuntimeError(f'خطای ارتباطی: پرداخت بدون مشتری={orphan_p}، ریزفاکتور بدون کالا={orphan_l}')
        base.commit(); base.execute('VACUUM'); base.commit()
        counts={t:base.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in [PERSONS,PAYMENTS,INVENTORY,INVTRAN] if table_exists(base,t)}
    finally:
        base.close()
        for ss in sources: ss.close()

    audit_path=Path(out).with_name(Path(out).stem+'_گزارش_ادغام.csv')
    with open(audit_path,'w',newline='',encoding='utf-8-sig') as f:
        w=csv.writer(f); w.writerow(['ترتیب منبع','عملیات','ID منبع','ID خروجی','شماره سند/جدول','جزئیات']); w.writerows(audit)
        if conflicts: w.writerow([]); w.writerow(['تعارض‌ها']); w.writerows(conflicts)
    if log: log(f'مشتری={counts.get(PERSONS,0)} | اسناد={counts.get(PAYMENTS,0)} | ریزفاکتور={counts.get(INVTRAN,0)} | تعارض={len(conflicts)}')
    return counts,conflicts,str(audit_path)

def validate_mks(path):

    c=connect(path)
    try:
        integrity=c.execute('PRAGMA integrity_check').fetchone()[0]
        if integrity != 'ok':
            raise RuntimeError(f'بررسی سلامت SQLite ناموفق بود: {integrity}')
        for t in (PERSONS, PAYMENTS, INVENTORY, INVTRAN):
            if not table_exists(c,t):
                raise RuntimeError(f'جدول ضروری در فایل نیست: {t}')
        orphan_p=c.execute(
            'SELECT COUNT(*) FROM payments p LEFT JOIN persons x ON x._id=p.personId '
            'WHERE p.personId IS NOT NULL AND x._id IS NULL'
        ).fetchone()[0]
        orphan_l=c.execute(
            'SELECT COUNT(*) FROM invenTran t LEFT JOIN inventory i ON i._id=t.articleId '
            'WHERE t.articleId IS NOT NULL AND i._id IS NULL'
        ).fetchone()[0]
        if orphan_p or orphan_l:
            raise RuntimeError(f'ارجاع ناقص: سند بدون مشتری={orphan_p}؛ ریزفاکتور بدون کالا={orphan_l}')
        return True
    finally:
        c.close()

def merge_three(files,out,log=None):

    files=[os.path.abspath(x) for x in files]
    out=os.path.abspath(out)
    if out in files:
        raise ValueError('فایل خروجی نباید با هیچ‌یک از فایل‌های ورودی یکسان باشد.')
    folder=os.path.dirname(out) or os.getcwd()
    os.makedirs(folder,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='._mks_build_',suffix='.mks',dir=folder)
    os.close(fd); os.remove(tmp)
    tmp_audit=None
    try:
        counts,conf,tmp_audit=_merge_three_impl(files,tmp,log)
        validate_mks(tmp)
        final_audit=str(Path(out).with_name(Path(out).stem+'_گزارش_ادغام.csv'))
        if os.path.exists(out):
            from datetime import datetime
            stamp=datetime.now().strftime('%Y%m%d_%H%M%S')
            backup=out+'.قبل_از_جایگزینی_'+stamp+'.bak'
            shutil.copy2(out,backup)
            if log: log('پشتیبان خروجی قبلی ساخته شد: '+backup)
        os.replace(tmp,out)
        if tmp_audit and os.path.exists(tmp_audit):
            os.replace(tmp_audit,final_audit)
        if log: log('کنترل سلامت SQLite و ارجاع‌های اصلی: موفق')
        return counts,conf,final_audit
    except Exception:
        for p in (tmp,tmp_audit):
            if p and os.path.exists(p):
                try: os.remove(p)
                except OSError: pass
        raise

