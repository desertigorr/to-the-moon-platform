import assert from 'node:assert/strict'
import test from 'node:test'
import {
  connectStream,
  streamUrl,
  TelemetryApi,
} from '../../src/features/telemetry/client.ts'
import { snapshot, vehicle } from './fixtures.mjs'

function harness() {
  const sockets = [],
    timers = new Map(),
    messages = [],
    statuses = [],
    errors = []
  let next = 0
  const stop = connectStream('http://localhost:8000/prefix/', {
    createSocket(url) {
      const s = {
        url,
        onopen: null,
        onmessage: null,
        onerror: null,
        onclose: null,
        closed: false,
        close() {
          this.closed = true
        },
      }
      sockets.push(s)
      return s
    },
    schedule(callback, delay) {
      const id = ++next
      timers.set(id, { callback, delay })
      return id
    },
    cancel(id) {
      timers.delete(id)
    },
    onMessage: (m) => messages.push(m),
    onConnection: (s) => statuses.push(s),
    onError: (e) => errors.push(e),
  })
  return {
    sockets,
    timers,
    messages,
    statuses,
    errors,
    stop,
    run(delay) {
      const found = [...timers].find(([, v]) => v.delay === delay)
      assert.ok(found, `timer ${delay}`)
      timers.delete(found[0])
      found[1].callback()
    },
  }
}
test('connection waits for snapshot, rejects malformed input and disposes socket/timers', () => {
  const h = harness(),
    s = h.sockets[0]
  assert.equal(s.url, 'ws://localhost:8000/prefix/api/stream')
  s.onopen()
  assert.equal(h.statuses.at(-1), 'connecting')
  s.onmessage({ data: 'broken' })
  assert.ok(h.errors.at(-1))
  assert.equal(h.messages.length, 0)
  s.onmessage({ data: JSON.stringify(snapshot([vehicle()])) })
  assert.equal(h.statuses.at(-1), 'connected')
  assert.equal(h.messages.length, 1)
  h.stop()
  assert.equal(s.closed, true)
  assert.equal(h.timers.size, 0)
  assert.equal(s.onmessage, null)
})
test('reconnect backoff is bounded, old sockets cannot publish, successful snapshot resets backoff', () => {
  const h = harness()
  for (const delay of [1000, 2000, 4000, 8000, 10000, 10000]) {
    const s = h.sockets.at(-1),
      staleCallback = s.onmessage
    s.onclose()
    assert.equal(h.statuses.at(-1), 'disconnected')
    staleCallback({ data: JSON.stringify(snapshot([vehicle()])) })
    assert.equal(h.messages.length, 0)
    h.run(delay)
  }
  const latest = h.sockets.at(-1)
  latest.onmessage({ data: JSON.stringify(snapshot([], 0)) })
  latest.onclose()
  h.run(1000)
  assert.equal(h.statuses.at(-1), 'connecting')
  h.stop()
  assert.equal(h.timers.size, 0)
})
test('silent connection has a bounded wait and reconnects', () => {
  const h = harness()
  h.run(30000)
  assert.equal(h.sockets[0].closed, true)
  assert.equal(h.statuses.at(-1), 'disconnected')
  h.run(1000)
  assert.equal(h.sockets.length, 2)
  h.stop()
})
test('URLs preserve base prefix and encode timezone offsets; HTTP errors are explicit', async () => {
  assert.equal(
    streamUrl('https://example.org/prefix/?a=1'),
    'wss://example.org/prefix/api/stream',
  )
  assert.throws(() => streamUrl('ftp://example.org'), /HTTP/)
  const original = globalThis.fetch,
    urls = []
  try {
    globalThis.fetch = async (url) => {
      urls.push(url.toString())
      return new Response(
        JSON.stringify({
          unit_id: 7,
          capacity: 300,
          retained_count: 0,
          count: 0,
          items: [],
        }),
        { status: 200 },
      )
    }
    const api = new TelemetryApi('https://example.org/base')
    await api.history(7, { until: '2026-09-26T12:00:00+03:00', limit: 60 })
    assert.ok(urls[0].includes('%2B03%3A00'))
    assert.ok(urls[0].includes('/base/api/vehicles/7/history'))
    globalThis.fetch = async () => new Response('', { status: 404 })
    await assert.rejects(() => api.vehicle(7), /не найдено/)
    globalThis.fetch = async () => new Response('', { status: 422 })
    await assert.rejects(() => api.history(7, { limit: 999 }), /параметры/)
  } finally {
    globalThis.fetch = original
  }
})
