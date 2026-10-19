#define PY_SSIZE_T_CLEAN
#define Py_LIMITED_API 0x030B0000
#include <Python.h>
#include <math.h>
#include <stdlib.h>
#include <float.h>
#include <string.h>

typedef struct { double a[3], b[3], c[3], lo[3], hi[3]; } Triangle;
typedef struct { double lo[3], hi[3]; size_t first, count, left, right; } Node;
typedef struct { Triangle *tri; Node *nodes; size_t used; } Tree;
#define CAPSULE "REasy.SdfTriangleTree"
#define MAX_TRIANGLES 1000000
#define MAX_POINTS 65536

static double dot(const double *a, const double *b) {
    return a[0]*b[0]+a[1]*b[1]+a[2]*b[2];
}
static void sub(const double *a, const double *b, double *r) {
    for (int i=0;i<3;i++) r[i]=a[i]-b[i];
}
static void cross(const double *a, const double *b, double *r) {
    r[0]=a[1]*b[2]-a[2]*b[1]; r[1]=a[2]*b[0]-a[0]*b[2]; r[2]=a[0]*b[1]-a[1]*b[0];
}
static int compare_axis(const Triangle *a, const Triangle *b, int k) {
    double x=a->lo[k]+a->hi[k], y=b->lo[k]+b->hi[k];
    return (x>y)-(x<y);
}
static int cmp_x(const void *a,const void *b) { return compare_axis(a,b,0); }
static int cmp_y(const void *a,const void *b) { return compare_axis(a,b,1); }
static int cmp_z(const void *a,const void *b) { return compare_axis(a,b,2); }
static size_t build_node(Tree *tree, size_t first, size_t count) {
    size_t id=tree->used++;
    Node *node=&tree->nodes[id];
    node->first=first; node->count=count;
    for(int k=0;k<3;k++) { node->lo[k]=DBL_MAX; node->hi[k]=-DBL_MAX; }
    for(size_t i=first;i<first+count;i++) for(int k=0;k<3;k++) {
        node->lo[k]=fmin(node->lo[k],tree->tri[i].lo[k]);
        node->hi[k]=fmax(node->hi[k],tree->tri[i].hi[k]);
    }
    if(count<=8) return id;
    int axis=0;
    for(int k=1;k<3;k++) if(node->hi[k]-node->lo[k]>node->hi[axis]-node->lo[axis]) axis=k;
    int (*comparators[3])(const void *,const void *)={cmp_x,cmp_y,cmp_z};
    qsort(tree->tri+first,count,sizeof(Triangle),comparators[axis]);
    node->count=0;
    node->left=build_node(tree,first,count/2);
    node->right=build_node(tree,first+count/2,count-count/2);
    return id;
}
static void destroy_tree(Tree *tree) {
    if(tree) { free(tree->tri); free(tree->nodes); free(tree); }
}
static void destroy_capsule(PyObject *object) {
    destroy_tree(PyCapsule_GetPointer(object,CAPSULE));
}
static PyObject *make_tree(PyObject *self, PyObject *object) {
    Py_buffer buffer;
    if(PyObject_GetBuffer(object,&buffer,PyBUF_CONTIG_RO|PyBUF_FORMAT)<0) return NULL;
    if(buffer.itemsize!=8 || !buffer.format || strcmp(buffer.format,"d") ||
       buffer.len<=0 || buffer.len%(9*8) || buffer.len/(9*8)>MAX_TRIANGLES) {
        PyBuffer_Release(&buffer);
        PyErr_SetString(PyExc_ValueError,"Expected 1..1000000 triangles as contiguous float64 coordinates");
        return NULL;
    }
    size_t count=(size_t)buffer.len/(9*8);
    Tree *tree=calloc(1,sizeof(Tree));
    if(tree) { tree->tri=malloc(count*sizeof(Triangle)); tree->nodes=malloc(2*count*sizeof(Node)); }
    if(!tree || !tree->tri || !tree->nodes) {
        PyBuffer_Release(&buffer); destroy_tree(tree); return PyErr_NoMemory();
    }
    const double *values=buffer.buf;
    int valid=1;
    /* Copy while holding the GIL: callers cannot mutate the input during build. */
    for(size_t i=0;i<count;i++) {
        Triangle *t=&tree->tri[i];
        for(int k=0;k<3;k++) {
            t->a[k]=values[i*9+k]; t->b[k]=values[i*9+3+k]; t->c[k]=values[i*9+6+k];
            if(!isfinite(t->a[k]) || !isfinite(t->b[k]) || !isfinite(t->c[k])) valid=0;
            t->lo[k]=fmin(t->a[k],fmin(t->b[k],t->c[k]));
            t->hi[k]=fmax(t->a[k],fmax(t->b[k],t->c[k]));
        }
        double ab[3],ac[3],n[3]; sub(t->b,t->a,ab); sub(t->c,t->a,ac); cross(ab,ac,n);
        if(!(dot(n,n)>0) || !isfinite(dot(n,n))) valid=0;
    }
    PyBuffer_Release(&buffer);
    if(!valid) { destroy_tree(tree); PyErr_SetString(PyExc_ValueError,"Non-finite or degenerate triangle"); return NULL; }
    Py_BEGIN_ALLOW_THREADS
    build_node(tree,0,count);
    Py_END_ALLOW_THREADS
    PyObject *result=PyCapsule_New(tree,CAPSULE,destroy_capsule);
    if(!result) destroy_tree(tree);
    return result;
}
static double box_distance(const Node *n,const double *p) {
    double d=0;
    for(int k=0;k<3;k++) { double x=fmax(n->lo[k]-p[k],fmax(p[k]-n->hi[k],0)); d+=x*x; }
    return d;
}
static double segment_distance(const double *p,const double *a,const double *b) {
    double ab[3],ap[3],r[3]; sub(b,a,ab); sub(p,a,ap);
    double u=fmin(1,fmax(0,dot(ap,ab)/dot(ab,ab)));
    for(int k=0;k<3;k++) r[k]=ap[k]-u*ab[k];
    return dot(r,r);
}
static double triangle_distance(const Triangle *t,const double *p) {
    double ab[3],ac[3],ap[3],n[3],v[3],w[3];
    sub(t->b,t->a,ab); sub(t->c,t->a,ac); sub(p,t->a,ap); cross(ab,ac,n);
    cross(ap,ac,v); cross(ab,ap,w);
    double nn=dot(n,n), s=dot(v,n), u=dot(w,n);
    if(s>=0 && u>=0 && s+u<=nn) { double d=dot(ap,n); return d*d/nn; }
    return fmin(segment_distance(p,t->a,t->b),
                fmin(segment_distance(p,t->b,t->c),segment_distance(p,t->c,t->a)));
}
static double nearest(const Tree *tree,size_t id,const double *p,double best) {
    const Node *n=&tree->nodes[id];
    if(box_distance(n,p)>best) return best;
    if(n->count) {
        for(size_t i=n->first;i<n->first+n->count;i++) best=fmin(best,triangle_distance(&tree->tri[i],p));
    } else {
        size_t a=n->left,b=n->right;
        if(box_distance(&tree->nodes[a],p)>box_distance(&tree->nodes[b],p)) { size_t tmp=a;a=b;b=tmp; }
        best=nearest(tree,a,p,best); best=nearest(tree,b,p,best);
    }
    return best;
}
static int ray_box(const Node *n,const double *p,const double *d) {
    double near=0,far=DBL_MAX;
    for(int k=0;k<3;k++) {
        double a=(n->lo[k]-p[k])/d[k],b=(n->hi[k]-p[k])/d[k];
        near=fmax(near,fmin(a,b)); far=fmin(far,fmax(a,b));
    }
    return near<=far;
}
/* Oriented crossings give nonzero winding, so overlapping consistently oriented shells count as inside. */
static int crossings(const Tree *tree,size_t id,const double *p,const double *d,int *ambiguous) {
    const Node *n=&tree->nodes[id]; int total=0;
    if(!ray_box(n,p,d)) return 0;
    if(!n->count) return crossings(tree,n->left,p,d,ambiguous)+crossings(tree,n->right,p,d,ambiguous);
    for(size_t i=n->first;i<n->first+n->count;i++) {
        const Triangle *t=&tree->tri[i]; double ab[3],ac[3],h[3],s[3],q[3];
        sub(t->b,t->a,ab);sub(t->c,t->a,ac);cross(d,ac,h);
        double det=dot(ab,h);
        if(fabs(det)<=1e-14*sqrt(dot(ab,ab)*dot(h,h))) continue;
        sub(p,t->a,s); double u=dot(s,h)/det;
        if(u< -1e-10 || u>1+1e-10) continue;
        cross(s,ab,q); double v=dot(d,q)/det;
        if(v< -1e-10 || u+v>1+1e-10 || dot(ac,q)/det<=0) continue;
        if(u<1e-10 || v<1e-10 || u+v>1-1e-10) { *ambiguous=1; continue; }
        total+=(det<0 ? 1:-1);
    }
    return total;
}
static PyObject *sample_tree(PyObject *self,PyObject *args) {
    PyObject *capsule,*object; int solid;
    if(!PyArg_ParseTuple(args,"OOp",&capsule,&object,&solid)) return NULL;
    Tree *tree=PyCapsule_GetPointer(capsule,CAPSULE);
    if(!tree) return NULL;
    Py_buffer buffer;
    if(PyObject_GetBuffer(object,&buffer,PyBUF_CONTIG_RO|PyBUF_FORMAT)<0) return NULL;
    if(buffer.itemsize!=8 || !buffer.format || strcmp(buffer.format,"d") ||
       buffer.len%(3*8) || buffer.len/(3*8)>MAX_POINTS) {
        PyBuffer_Release(&buffer); PyErr_SetString(PyExc_ValueError,"Expected at most 65536 float64 XYZ points"); return NULL;
    }
    size_t count=(size_t)buffer.len/(3*8);
    double *points=malloc(count*3*sizeof(double));
    if(count && !points) { PyBuffer_Release(&buffer); return PyErr_NoMemory(); }
    memcpy(points,buffer.buf,count*3*sizeof(double)); PyBuffer_Release(&buffer);
    for(size_t i=0;i<count*3;i++) if(!isfinite(points[i])) {
        free(points);PyErr_SetString(PyExc_ValueError,"Non-finite query point");return NULL;
    }
    PyObject *result=PyBytes_FromStringAndSize(NULL,(Py_ssize_t)(count*sizeof(float)));
    if(!result) {free(points);return NULL;}
    float *out=(float *)PyBytes_AsString(result);
    int failed=0;
    const double directions[3][3]={{1,0.3713906763541037,0.529129398},{0.238729318,1,0.682910347},{0.593723931,0.127382913,1}};
    Py_BEGIN_ALLOW_THREADS
    for(size_t i=0;i<count;i++) {
        const double *p=points+3*i; double distance=sqrt(nearest(tree,0,p,DBL_MAX));
        if(solid && distance>0) {
            int ambiguous=0,winding=0;
            for(int ray=0;ray<3;ray++) {
                ambiguous=0;winding=crossings(tree,0,p,directions[ray],&ambiguous);
                if(!ambiguous) break;
            }
            if(ambiguous) {failed=1;break;}
            if(winding) distance=-distance;
        }
        if(!isfinite(distance) || fabs(distance)>FLT_MAX) {failed=1;break;}
        out[i]=(float)distance;
    }
    Py_END_ALLOW_THREADS
    free(points);
    if(failed) {Py_DECREF(result);PyErr_SetString(PyExc_ValueError,"Ambiguous solid intersection or distance overflow; bake was not applied");return NULL;}
    return result;
}
static PyMethodDef methods[]={
    {"build",make_tree,METH_O,"Build a triangle BVH from float64 triangle coordinates."},
    {"sample",sample_tree,METH_VARARGS,"Evaluate exact triangle distances, optionally signed by solid winding."},
    {NULL,NULL,0,NULL}
};
static struct PyModuleDef module={PyModuleDef_HEAD_INIT,"fastsdftex",NULL,-1,methods};
PyMODINIT_FUNC PyInit_fastsdftex(void) {return PyModule_Create(&module);}
