# The container's soft limit is 1024, and one download task holds a socket per
# segment plus an open handle per destination file; a bulk of a few hundred
# files sits just under it. The hard limit is 524288, so the soft one can be
# raised from here. It has to be here and not only in docker-compose.yml: a
# ulimits entry needs the container to be recreated, and /app is not a volume,
# so recreating would take config.py, the session and the downloads with it.
ulimit -n 65536

source mltbenv/bin/activate
python3 update.py
python3 -m bot
