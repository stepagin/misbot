FROM oci.element.io/synapse@sha256:6b84a7bbac36f080b2d2e51e0289cf1b08b349598ea44a558df38d558f2c2311
USER root
RUN python -m pip install --no-cache-dir synapse-s3-storage-provider==1.7.0 \
    && python -c 'import s3_storage_provider'
