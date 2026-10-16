"""Use the upstream provider with bounded upload concurrency on small VPSes."""

import os

from boto3.s3.transfer import TransferConfig
from s3_storage_provider import S3StorageProviderBackend as Upstream


class S3StorageProviderBackend(Upstream):
    async def store_file(self, path, file_info):
        return await self._module_api.defer_to_threadpool(
            self._s3_pool,
            self._get_s3_client().upload_file,
            Filename=os.path.join(self.cache_directory, path),
            Bucket=self.bucket,
            Key=self.prefix + path,
            ExtraArgs=self.extra_args,
            Config=TransferConfig(
                multipart_chunksize=8 * 1024 * 1024,
                max_concurrency=1,
                use_threads=False,
                preferred_transfer_client='classic',
            ),
        )
