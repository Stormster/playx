import asyncio, struct, sys, uuid
import aiohttp

async def main():
    run = 'test-' + uuid.uuid4().hex[:10]
    base = 'http://127.0.0.1:8799'
    async with aiohttp.ClientSession() as s:
        for path, headers in [('/pub', {'authorization':'Bearer wrong'}), ('/ws', {'origin':'https://forbidden.example'})]:
            try:
                await s.ws_connect(base + path + '?run=' + run, headers=headers)
                raise AssertionError('unauthorized connection succeeded')
            except aiohttp.WSServerHandshakeError as e:
                assert e.status == 403, e
        pub = await s.ws_connect(base + '/pub?run=' + run, headers={'authorization':'Bearer local-test-only'})
        viewer = await s.ws_connect(base + '/ws?run=' + run, headers={'origin':'http://127.0.0.1:8787'})
        packet = b'\x01\x00\x00' + struct.pack('<I', 42) + b'fake-video'
        await pub.send_bytes(b'\x10' + struct.pack('<I', len(packet)) + packet)
        assert (await viewer.receive(timeout=2)).data[0] == 1
        await pub.send_bytes(b'\x11')
        assert (await viewer.receive(timeout=2)).data == b'\x03'
        late = await s.ws_connect(base + '/ws?run=' + run, headers={'origin':'http://127.0.0.1:8787'})
        first = (await late.receive(timeout=2)).data
        assert first[0] in (2,4), first
        if first[0] == 2:
            assert (await late.receive(timeout=2)).data == b'\x03'
        await pub.close(); await viewer.close(); await late.close()
    print('PASS relay auth/origin refusals, live forwarding, freeze marker, frozen late-join snapshot')
asyncio.run(main())
