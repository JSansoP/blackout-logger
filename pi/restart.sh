#!/bin/bash
sudo systemctl restart blackout-uptime
sudo systemctl status blackout-uptime --no-pager -l
