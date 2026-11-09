'use strict';
const CACHE='ksu-offline-release-shell-__BUILD__';
const SHELL=['/offline-visit/','/offline-visit/index.html','/offline-visit/app.js','/offline-visit/transport.js','/offline-visit/store.js','/offline-visit/ksu.css','/offline-visit/prototype.css','/offline-visit/manifest.webmanifest','/offline-visit/icon.svg'];
self.addEventListener('install',event=>event.waitUntil((async()=>{const cache=await caches.open(CACHE);try{for(const url of SHELL){const response=await fetch(url,{cache:'reload',credentials:'omit'});if(!response.ok)throw Error('Incomplete shell');await cache.put(url,response)}}catch(error){await caches.delete(CACHE);throw error}})()));
self.addEventListener('message',event=>{if(event.data?.type==='ACTIVATE')self.skipWaiting()});
self.addEventListener('activate',event=>event.waitUntil(self.clients.claim()));
self.addEventListener('fetch',event=>{const url=new URL(event.request.url);if(event.request.method==='GET'&&url.origin===self.location.origin&&SHELL.includes(url.pathname)&&!url.search)event.respondWith((async()=>{const cache=await caches.open(CACHE);return await cache.match(url.pathname)||fetch(event.request)})())});
