'use strict';
const BUILD='__BUILD__',CACHE='ksu-stage0-shell-'+BUILD;
const SHELL=['/','/index.html','/app.js','/store.js','/ksu.css','/prototype.css','/manifest.webmanifest','/icon.svg'];
self.addEventListener('install',event=>event.waitUntil((async()=>{const cache=await caches.open(CACHE);try{for(const url of SHELL){const response=await fetch(url,{cache:'reload',credentials:'omit'});if(!response.ok)throw Error('Incomplete shell');await cache.put(url,response)}}catch(error){await caches.delete(CACHE);throw error}})()));
// Waiting worker is activated only by the unlocked page after a successful local save.
self.addEventListener('message',event=>{if(event.data?.type==='ACTIVATE')self.skipWaiting();if(event.data?.type==='VERSION')event.ports[0]?.postMessage({build:BUILD})});
self.addEventListener('activate',event=>event.waitUntil((async()=>{await self.clients.claim();/* Stage 0 retains old shell caches; no user data lives here. */})()));
self.addEventListener('fetch',event=>{
  const url=new URL(event.request.url);
  if(event.request.method!=='GET'||url.origin!==self.location.origin)return;
  // No wildcard navigation fallback, no API, packages or photos in Cache API.
  if(SHELL.includes(url.pathname)&&!url.search)event.respondWith((async()=>{const cache=await caches.open(CACHE);return await cache.match(url.pathname)||fetch(event.request)})());
});
