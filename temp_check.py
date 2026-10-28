import os,sys
sys.path.insert(0, r'C:\Users\ramro\OneDrive\Documents\mock interview standalone\MOCK-INTERVIEW-STANDALONE')
os.environ['DJANGO_SETTINGS_MODULE'] = 'mock_interview_site.settings'
import django
django.setup()
from django.db import connection
c = connection.cursor()
c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='mock_interview_interviewreport'")
r = c.fetchone()
if r:
    print(r[0])
else:
    print('NONE')
