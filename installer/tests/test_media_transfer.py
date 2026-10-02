"""Lost responses and collisions must preserve both local and existing remote data."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import media_transfer


class Missing(Exception):
    response={'Error':{'Code':'NoSuchKey'}}


class S3:
    def __init__(self):self.objects={};self.puts=0;self.lose_response=False
    def head_bucket(self,**kwargs):pass
    def get_object(self,Bucket,Key):
        if Key not in self.objects:raise Missing()
        return {'Body':io.BytesIO(self.objects[Key])}
    def put_object(self,Bucket,Key,Body,Metadata):
        self.objects[Key]=Body.read();self.puts+=1
        if self.lose_response:
            self.lose_response=False
            raise TimeoutError('response lost after server commit')


class MediaTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.media=self.root/'media'
        self.media.mkdir()
        (self.media/'one').write_bytes(b'example encrypted attachment')
        self.checkpoint=self.root/'checkpoint.json'
        self.values={'S3_BUCKET':'example-bucket','S3_REGION':'example-region',
                     'S3_ENDPOINT_URL':'https://objects.example.org','S3_PREFIX':'test/'}
        self.service=S3()
    def tearDown(self):self.temp.cleanup()
    def test_lost_put_response_resumes_without_duplicate_or_deletion(self):
        self.service.lose_response=True
        with self.assertRaises(TimeoutError):
            media_transfer.transfer(self.media,self.values,self.checkpoint,self.service)
        self.assertEqual(json.loads(self.checkpoint.read_text())['files']['one']['status'],'uploading')
        result=media_transfer.transfer(self.media,self.values,self.checkpoint,self.service)
        self.assertEqual(self.service.puts,1)
        self.assertEqual(result['files_verified'],1)
        self.assertTrue((self.media/'one').is_file())
    def test_remote_collision_never_overwrites_existing_object(self):
        self.service.objects['test/one']=b'existing different data'
        with self.assertRaisesRegex(RuntimeError,'nothing overwritten'):
            media_transfer.transfer(self.media,self.values,self.checkpoint,self.service)
        self.assertEqual(self.service.objects['test/one'],b'existing different data')
        self.assertEqual(self.service.puts,0)
    def test_repeat_rechecks_remote_content_instead_of_trusting_checkpoint(self):
        media_transfer.transfer(self.media,self.values,self.checkpoint,self.service)
        self.service.objects['test/one']=b'corrupted remote data'
        with self.assertRaisesRegex(RuntimeError,'different data'):
            media_transfer.transfer(self.media,self.values,self.checkpoint,self.service)

    def test_symlinked_media_root_is_refused_before_upload(self):
        alias=self.root/'alias';alias.symlink_to(self.media,target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError,'no outside files'):
            media_transfer.transfer(alias,self.values,self.checkpoint,self.service)
        self.assertEqual(self.service.puts,0)
        self.assertFalse(self.checkpoint.exists())
