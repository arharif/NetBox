from extras.scripts import Script

class TestScript(Script):

    class Meta:
        name = 'Test Script'
        description = 'Simple NetBox script test'

    def run(self, data, commit):
        self.log_success('The test script works.')
        return 'OK'
