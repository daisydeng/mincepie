"""Simple Matlab mapper

This is a simple matlab mapper that you can use to deal with some legacy code
that still requires Matlab. It is not very fancy and complex - no Matlab 
engines are required for this, and all you need is the matlab command, as well
as python's subprocess module. 

The downside is that each map() function starts Matlab and closes it, which
causes additional overhead (several seconds, based on your Matlab config).
Thus, it should mostly be used to carry out computation-intensive map tasks
each lasting more than a few seconds. If your map() runs much shorter than
the Matlab start and finish overhead, it's probably not a good idea to
distribute your job.
"""

from mincepie import mapreducer
from subprocess import Popen, PIPE

_config = {'matlab_bin': 'matlab',
           'args': ['-nodesktop','-nosplash','-nojvm','-singleCompThread']
          }
_SUCCESS_STR = '__mincepie.matlab.success__'
_FAIL_STR = '__mincepie.matlab.fail__'


def set_config(key, value):
    """Sets the config of matlab
    
    For example, you can set your own matlab bin:
    set_config('matlab_bin','/path/to/your/matlab/bin/matlab')
    """
    _config[key] = value


def wrap_command(command):
    """ We wrap the command in a try-catch pair. 
    
    If any exception is caught,
    we ask matlab to dump _FAIL_STR. Otherwise, matlab dumps _SUCCESS_STR.
    The returned string is then scanned by the mapper to determine the result
    of the mapreduce run
    """
    if type(command) is not list:
        command = [command]
    return ";\n".join(["try"] + command + [
        "fprintf(2,'%s')" % (_SUCCESS_STR)
        "catch ME",
        "disp(ME)",
        "disp(ME.stack)",
        "fprintf(2,'%s')" % (_FAIL_STR),
        "end",
        ])


SimpleMatlabMapper(mapreducer.BasicMapper):
    """The class that performs wordcount map
    
    The input value of this mapper should be a string containing the words
    to be counted
    """
    def make_command(self, key, value):
        """Make the Matlab command. You need to implement this in your code
        
        Example:
            def make_command(self, key, value):
                return ["fprintf('%s: %s\n')" % (key, value)]
        """
        raise NotImplementedError

    def map(self, key, value):
        """ The map function of SimpleMatlabMapper and its derivatives.

        Do NOT override this with your own map() function - instead, write
        your own make_command(self, key, value) function.
        """
        command = wrap_command(self.make_command(key, vale))
        try:
            proc = Popen([_config['matlab_bin']] + _config['args'],
                         stdin = PIPE, stdout = PIPE, stderr = PIPE)
        except OSError, errmsg:
            # if we catch OSError, we return the error for investigation
            yield key, (False, errmsg)
        # pass the command to Matlab. 
        try:
            str_out, str_err = proc.communicate(command)
        except Exception, errmsg:
            # if proc.communicate encounters some error, return the error
            yield key, (False, errmsg)
        # now, parse stderr to see whether we succeeded
        if str_err.endswith(_SUCCESS_STR):
            yield key, (True, str_out, str_err)
        else:
            yield key, (False, str_out, std_err)

mapreducer.REGISTER_MAPPER(SimpleMatlabMapper)


SimpleMatlabReducer = mapreducer.IdentityReducer
mapreducer.REGISTER_REDUCER(SimpleMatlabReducer)


if __name__ == "__main__":
    launcher.launch()