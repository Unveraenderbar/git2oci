#!/bin/env python3

# pylint: disable=broad-exception-caught too-few-public-methods too-many-instance-attributes

import functools, http.server, json, os, re, signal, subprocess, sys, traceback

# https://semver.org/#faq # pylint: disable=line-too-long
SEMVER_RE = r'^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)(?:-(?P<prerelease>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(?:\+(?P<buildmetadata>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$'
SHELL_CMD = ['sh', '+i', '+m', '+s', '-euo', 'pipefail'] # options crucial for reading input / catching errors in complex commands!

class KubePodCmd:

    __caConfigMap    = os.environ.get('GIT2OCI_CABUNDLE_CONFIGMAP'    , 'config-trusted-cabundle')
    __caConfigMapKey = os.environ.get('GIT2OCI_CABUNDLE_CONFIGMAP_KEY', 'ca-bundle.crt'          )
    __caSubPath      = f'{__caConfigMap}.crt'
    caMountPath      = f'/etc/ssl/certs/{__caSubPath}'
    __envs           = [{"name": "HTTP_PROXY"}, {"name": "HTTPS_PROXY"}, {"name": "http_proxy"}, {"name": "https_proxy"}]

    def __init__(self, name:str, cmd:str, image:str, addEnvs:list[dict]=None) -> None:
        self.__name, self.__image = name, image
        # patch type is crucial for input to be collected from / passed to container in pod, default "merge' does *not* work!
        self.__override           = '--overrides=' + json.dumps({
          'metadata'        : {'annotations': {'sidecar.istio.io/inject': 'false'}, 'labels': {'app': 'git2oci-run'}}
        , 'spec'            : {
              'containers': [{
                'command'     : SHELL_CMD + ['-c', cmd]
              , 'env'         : self.__envs + (addEnvs if addEnvs else [])
              , 'name'        : name
              , 'resources'   : {'requests': {'memory': '50Mi','cpu': '50m'}}
              , 'volumeMounts': [{
                                      'mountPath': self.caMountPath
                                   ,  'name'     : self.__caConfigMap
                                   ,  'subPath'  : self.__caSubPath
                                 }]
               }]
          , 'serviceAccount': 'default' # run dehydration+push operations with reduced privileges
          , 'volumes'       : [{
                          'name'     : self.__caConfigMap
                        , 'configMap': {
                            'name' : self.__caConfigMap
                          , 'items': [{'key': self.__caConfigMapKey, 'path': self.__caSubPath}]
                        }
            }]
          }
        })

    def __call__(self) -> str:
        # order of options important – "--overrides=..." has to appear before "--restart" and "--stdin"!!
        # * JSON spec overrides have to be used in order to add HW resource requests
        # * output of shell command in the pod is collected+printed by kubectl only *iff*
        #   * command is given in JSON overrrides
        #   * restart policy "Never" must be specified – but not in the JSON overrides, only as kubectl command line argument
        return f'kubectl run --attach --override-type=strategic --restart=Never --quiet --stdin' \
               f" --image '{self.__image}' '{self.__override}' {self.__name}"


class ProcessSourceChange:

    __hydrate:str        = os.environ.get('GIT2OCI_DEHYDRATION_PATHNAME', './hydrate')
    __imageRegistry:str  = os.environ.get('GIT2OCI_IMAGE_REGISTRY', '')
    __imagePathRe:str    = re.sub(r"(^['\"]|['\"]$)", '', os.environ.get('GIT2OCI_IMAGE_PATH_RE', '')) # might be quoted
    __pusherImage:str    = os.environ.get('GIT2OCI_PUSHER_IMAGE', '')
    __runnerImage:str    = os.environ.get('GIT2OCI_RUNNER_IMAGE', '')
    __targetRegistry:str = os.environ.get('GIT2OCI_TARGET_REGISTRY', '')
    __runnerCmd:str      = 'mkdir /tmp/git2oci; cd /tmp/git2oci; curl -s {archive} | tar zxf -; exec {path} {cluster} {namespace}'

    def __initRevision(self, metadata:dict) -> None: # path from event/GitRepository CR overrides path in git tag build ID!
        self.gitRef:str            = metadata['revision']
        semVer, self.revision      = self.gitRef.split('@sha1:')
        assert re.match(SEMVER_RE, semVer), f'revision {semVer} does not comply with Semantic Versioning Specification 2.0.0'
        semVerComponents:list[str] = semVer.split('+')
        imageTag, semVerBuildNo    = semVerComponents if len(semVerComponents) > 1 else (semVer, '')
        metadataImagePath:str      = metadata.get('git2oci-imagePath'  , ''            ) # transferred from GitRepository CR...
        self.hydratePath:str       = metadata.get('git2oci-hydratePath', self.__hydrate) # ... annotations to events by Flux
        self.imagePath:str         = metadataImagePath if metadataImagePath else semVerBuildNo.replace('.', '/')
        self.summary:str           = metadata['summary']
        self.targetImg:str         = f'oci://{self.__targetRegistry}/{self.imagePath}:{imageTag}'
        assert   self.imagePath, f'cannot extract path for OCI image in registry from event metadata {metadata}'
        assert   re.match(self.__imagePathRe, self.imagePath) \
               , f'image path {self.imagePath} in revision {self.gitRef} does not match {self.__imagePathRe}'

    def __init__(self, jsonRequest:dict, requestHandler) -> None:
        assert self.__imageRegistry and self.__targetRegistry, 'registry configuration missing from environment'
        assert self.__pusherImage   and self.__runnerImage   , 'runner or pusher image configuration missing from environment'
        self.__requestHandler:JsonPostHandler = requestHandler
        try:
            self.kind:str      = jsonRequest['involvedObject']['kind']
            self.artifact:str  = jsonRequest['involvedObject']['name']
            self.eventUid:str  = jsonRequest['involvedObject']['uid' ]
            self.__initRevision( jsonRequest['metadata'      ]        )
            self.reason:str    = jsonRequest['reason'        ]
        except KeyError as e:
            self.__requestHandler.errorReponse(f'Server error invalid/incomplete request {jsonRequest}: {str(e)}', 500)
            raise RuntimeError(f'invalid/incomplete request {jsonRequest}: {str(e)}') from e
        action:str             = self.__runRequest() if self.reason == "NewArtifact" and self.imagePath else 'skip'
        self.result:dict       = {  'status' : 'success'
                                  , 'message': f'artifact {self.artifact} new ref {self.gitRef}, {action}'}

    def __runCmd(self, cmd:str, inputBytes:bytes=b'') -> str:
        args, result                       = SHELL_CMD + ['-c', cmd], None
        self.__requestHandler.log_message(f'running command ({len(inputBytes)} characters input): {cmd}')
        result:subprocess.CompletedProcess = subprocess.run(args, bufsize=0, capture_output=True, check=False, input=inputBytes)
        if result.returncode:
            raise RuntimeError(f'command {cmd} failed (exit code {result.returncode}): {result.stderr.decode("utf-8")}')
        return result.stdout.decode('utf-8')

    def __runnerArgs(self) -> tuple[str]: # constrain paths to valid project+cluster+namespace combinations?
        try:
            args:list[str] = self.imagePath.split('/')[-3:-1] # expect: <org>/<project>/config/<cluster>/<namespace>/<component>
        except Exception:
            args:list[str] = [os.environ.get('GIT2OCI_TARGET_CLUSTER', ''), '']
        return tuple(args)

    def __runRequest(self) -> None:
        jsonPath:str       = '"jsonpath={.spec.url}!!{.status.artifact.url}"'
        repo, archive      = self.__runCmd(f'kubectl get -o {jsonPath} {self.kind} {self.artifact}').split('!!')
        cluster, namespace = self.__runnerArgs()
        assert   self.imagePath.split('/')[-1] == repo.split('/')[-1].split('.')[0] \
               , f'git repository name {self.imagePath} from event does not correlate to build ID in revision {self.gitRef}'
        runnerCmd:str      = self.__runnerCmd.format(archive=archive, path=self.hydratePath, cluster=cluster, namespace=namespace)
        runnerImage:str    = self.__imageRegistry + '/' + os.environ.get('GIT2OCI_RUNNER_IMAGE', '')
        return self.__pushImg(repo, self.__runCmd(KubePodCmd(f'git2oci-run-{self.eventUid}', runnerCmd, runnerImage)()))

    def __pushImg(self, sourceRepo:str, k8sResources:bytes) -> str:
        credEnvNames:list[str] = ['GIT2OCI_TARGET_REGISTRY_USERNAME', 'GIT2OCI_TARGET_REGISTRY_PASSWORD']
        assert   all(os.environ.get(n, '') for n in credEnvNames) \
               , f'environment variable {" or ".join(credEnvNames)} for registry authentification missing'
        credsArg:str           = ':'.join('$' + n for n in credEnvNames)
        credEnvs:list[dict]    = [ {"name": n, "value": os.environ[n]} for n in credEnvNames ]
        assert len(k8sResources), f'no resources to push for git repository {sourceRepo}'
        pusherCmd:str          = f'exec flux push artifact {self.targetImg} -o=json --path=- --reproducible' \
                                 f' --revision={self.revision} --source={sourceRepo}' \
                                 f' --certificate-authority={KubePodCmd.caMountPath} --creds={credsArg}'
        pusherImage            = self.__imageRegistry + '/' + os.environ.get('GIT2OCI_PUSHER_IMAGE', '')
        pusherKubePodCmd:str   = KubePodCmd(f'git2oci-push-{self.eventUid}', pusherCmd, pusherImage, addEnvs=credEnvs)
        runOrLog               = self.__requestHandler.log_message if self.summary.find('no-dry-run') == -1 else \
                                 functools.partial(self.__runCmd, inputBytes=k8sResources.encode('utf-8'))
        self.__requestHandler.log_message('pushed image ' + runOrLog(pusherKubePodCmd()))
        return f'built and pushed OCI image {self.targetImg} ({len(k8sResources)} bytes)'


class JsonPostHandler(http.server.BaseHTTPRequestHandler):

    def jsonResponse(self, jsonRepsonse:dict, responseStatus:int=200) -> None:
        self.send_response(responseStatus)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        response:str = json.dumps(jsonRepsonse)
        (self.log_message if responseStatus < 400 else self.log_error)(f'Response JSON data {response}')
        self.wfile.write(response.encode('utf-8')) # returns None

    def errorReponse(self, msg:str, responseStatus:int=400):
        self.jsonResponse( {'error': msg} , responseStatus) # returns None

    def readPostJson(self) -> dict:
        contentLength:int = int(self.headers.get('Content-Length', -1))
        assert contentLength > 0, RuntimeError('no data provided (Content-Length is {contentLength}')
        return json.loads(self.rfile.read(contentLength).decode('utf-8'))

    def do_GET(self) -> None:
        self.log_message(f'Received GET request for path {self.path}')
        if self.path in ['/health', '/ready']:
            self.jsonResponse({'status': ('ready' if self.path == '/ready' else 'healthy')})
        else:
            self.errorReponse('Only POST requests can be processed') # HTTP GET not implemented here

    def do_POST(self) -> None:
        try:
            jsonData:dict = self.readPostJson()
            self.log_message(f'Received JSON data: {json.dumps(jsonData)}')
            self.jsonResponse(ProcessSourceChange(jsonData, self).result) # returns None
        except json.JSONDecodeError:
            self.errorReponse('Server error: invalid JSON in request')
        except Exception as e:
            self.errorReponse(f'Server error {traceback.format_tb(sys.exc_info()[2])[-1]}: {str(e)}', 500)


def serve(server_class=http.server.HTTPServer, handler_class=JsonPostHandler, port=8000) -> None:
    _                            = [ signal.signal(s, lambda: sys.exit(0)) for s in [signal.SIGINT, signal.SIGTERM]]
    print(f'Starting server on port {port}...')
    httpd:http.server.HTTPServer = server_class(('', port), handler_class) # listen on all available interfaces
    httpd.serve_forever()
    httpd.server_close()
    print('HTTP server stopped.')


if __name__ == '__main__':
    serve()
