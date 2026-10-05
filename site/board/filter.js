'use strict';
(function(root){
  const normalize=s=>String(s??'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLowerCase();
  function matches(r,f,text=r.search||''){
    if(f.company&&r.company!==f.company||f.brand&&r.brand!==f.brand||f.catalog&&!r.catalogs.includes(f.catalog)||f.collection&&!r.collections.includes(f.collection)&&r.id!==f.collection||f.evidence&&r.evidence!==f.evidence)return false;
    const values=r.dateValues||[];
    if(f.year==='unknown'&&values.length)return false;
    if(f.year&&f.year!=='unknown'||f.month||f.from||f.to){
      if(!values.some(v=>{
        const year=v.slice(0,4),month=v.length>=7?v.slice(5,7):'';
        // A year-only source cannot establish a month or a monthly range boundary.
        if(f.year&&f.year!=='unknown'&&year!==String(f.year)||f.month&&month!==String(f.month).padStart(2,'0'))return false;
        if((f.from?.length>4||f.to?.length>4)&&!month)return false;
        return (!f.from||v.slice(0,f.from.length)>=f.from)&&(!f.to||v.slice(0,f.to.length)<=f.to);
      }))return false;
    }
    return !f.query||f.query.split(/\s+/).every(t=>text.includes(t));
  }
  const api={normalize,matches};if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.AmonFilter=api;
})(globalThis);
