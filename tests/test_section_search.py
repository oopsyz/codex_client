import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/codex-ws-client/scripts'))
from codex_ws_client import ProtocolClient


class SectionSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovers_empty_preview_and_deduplicates_across_pages(self):
        client = ProtocolClient(None)
        ordinary = {'id': 'ordinary', 'name': 'Engine', 'preview': 'text'}
        hidden = {'id': 'hidden', 'name': 'Engine', 'preview': ''}
        client.list_threads = AsyncMock(side_effect=[
            {'data': [ordinary], 'nextCursor': 'ordinary-page-2'},
            {'data': [], 'nextCursor': None},
            {'data': [ordinary, hidden], 'nextCursor': 'section-page-2'},
            {'data': [hidden], 'nextCursor': None},
        ])
        client.request = AsyncMock(side_effect=[
            {'data': [], 'nextCursor': 'sections-page-2'},
            {'data': [{'id': 'pinned'}], 'nextCursor': None},
        ])
        result = await client.search_threads(5, title='Engine', cwd='C:/repo')
        self.assertEqual(result['data'], [ordinary, hidden])
        self.assertIsNone(result['nextCursor'])
        self.assertIn('unsectioned', result['limitation'])
        self.assertEqual(client.list_threads.call_args_list[1].kwargs['cursor'], 'ordinary-page-2')
        self.assertEqual(client.request.call_args_list[1].args[1]['cursor'], 'sections-page-2')
        self.assertEqual(client.list_threads.call_args_list[3].kwargs['cursor'], 'section-page-2')
        for call in client.list_threads.call_args_list:
            self.assertEqual(call.kwargs['title'], 'Engine')
            self.assertEqual(call.kwargs['cwd'], 'C:/repo')

    async def test_repeated_cursor_does_not_report_partial_success(self):
        client = ProtocolClient(None)
        client.list_threads = AsyncMock(return_value={'data': [], 'nextCursor': 'same'})
        with self.assertRaisesRegex(ValueError, 'repeated a cursor'):
            await client.search_threads(5, title='Engine')

    async def test_section_failure_is_not_an_empty_search_success(self):
        client = ProtocolClient(None)
        client.list_threads = AsyncMock(return_value={'data': [], 'nextCursor': None})
        client.request = AsyncMock(side_effect=RuntimeError('unsupported section listing'))
        with self.assertRaisesRegex(RuntimeError, 'unsupported'):
            await client.search_threads(5, title='Engine')

    async def test_rejects_foreign_pagination_cursor(self):
        client = ProtocolClient(None)
        with self.assertRaisesRegex(ValueError, 'initial search'):
            await client.search_threads(5, title='Engine', cursor='ordinary-only')
