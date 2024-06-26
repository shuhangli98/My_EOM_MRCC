import itertools
import functools
import time
import numpy as np
import copy
import psi4
import forte
import forte.utils
from forte import forte_options
import scipy
from functools import wraps
import math

def cc_residual_equations(op,ref,ham_op,exp_op,maxk,screen_thresh_H,screen_thresh_exp):
    """This function implements the CC residual equation

    Parameters
    ----------
    op : SparseOperator
        The cluster operator
    ref : StateVector
        The reference wave function
    ham_op : SparseHamiltonian
        The Hamiltonian operator
    exp_op : SparseExp
        The exponential operator        

    Returns
    -------
    tuple(list(float),float)
        A tuple with the residual and the energy
    """    
    
    # Step 1. Compute exp(S)|Phi>
    wfn = exp_op.compute(op,ref,scaling_factor=1.0,maxk=maxk,screen_thresh=screen_thresh_exp)
    
    # Step 2. Compute H exp(S)|Phi>
    Hwfn = ham_op.compute(wfn,screen_thresh_H)
    
    # Step 3. Compute exp(-S) H exp(S)|Phi>
    R = exp_op.compute(op,Hwfn,scaling_factor=-1.0,maxk=maxk,screen_thresh=screen_thresh_exp)
    
    # Step 4. Project residual onto excited determinants: <Phi^{ab}_{ij}|R>
    residual = forte.get_projection(op,ref,R)
    energy = forte.overlap(ref,R)

    return (residual, energy)

def cc_residual_equations_truncated(op,ref,ham_op,screen_thresh_H, n_comm):
    # This function is used to test the effect of truncation on the BCH expansion.
    Hwfn = ham_op.compute(ref,screen_thresh_H)
    residual = forte.get_projection(op,ref,Hwfn)
    residual = np.array(residual)
    energy = forte.overlap(ref,Hwfn)
    for k in range(1, n_comm+1):
        for l in range(k+1):
            wfn_comm = ref
            m = k - l
            for i in range(m):
                wfn_comm = forte.apply_operator(op,wfn_comm)
            wfn_comm = ham_op.compute(wfn_comm,screen_thresh_H)
            for i in range(l):
                wfn_comm = forte.apply_operator(op,wfn_comm)
            if (l%2 == 0):
                residual += np.array(forte.get_projection(op,ref,wfn_comm)) / (math.factorial(l) * math.factorial(m))
                energy += forte.overlap(ref,wfn_comm) / (math.factorial(l) * math.factorial(m))
            else:
                residual -= np.array(forte.get_projection(op,ref,wfn_comm)) / (math.factorial(l) * math.factorial(m))
                energy -= forte.overlap(ref,wfn_comm) / (math.factorial(l) * math.factorial(m))
            
    
    return (residual, energy)
    

def cc_variational_functional(t, op, ref, ham_op, exp_op, screen_thresh_H, screen_thresh_exp, maxk):
    '''
    E[A] = <Psi|exp(-A) H exp(A)|Psi>
    '''
    op.set_coefficients(t)
    # Step 1. Compute exp(S)|Phi>
    wfn = exp_op.compute(op,ref,scaling_factor=1.0,maxk=maxk,screen_thresh=screen_thresh_exp)
    
    # Step 2. Compute H exp(S)|Phi>
    Hwfn = ham_op.compute(wfn,screen_thresh_H)
    
    # Step 3. Compute exp(-S) H exp(S)|Phi>
    R = exp_op.compute(op,Hwfn,scaling_factor=-1.0,maxk=maxk,screen_thresh=screen_thresh_exp)
    
    # Step 4. Get the energy: <Phi|R>
    # E = <ref|R>, R is a StateVector, which can be looked up by the determinant
    energy = forte.overlap(ref,R)
    
    norm = forte.overlap(wfn, wfn)
    return energy/norm
    
def update_amps_orthogonal(residual,denominators, op, t, P, S, X, numnonred, update_radius=0.01, eta=0.1, diis = None):    
    namps = S.shape[0]
    
    # 1. Form the residual in the orthonormal basis
    R1 = X.T @ residual
    
    # 2. Form the M in the nonredundant space
    # M = X^+ S 
    M = X.T @ S 
    # M = X^+ S Delta
    for i in range(namps):
        for j in range(namps):
            M[i][j] *= (denominators[j] + eta)
    # M = X^+ S Delta X
    M = M @ X
    
    # 3. Form the r' vector in the nonredundant space
    kn = -R1[:numnonred]
    # Form the M matrix in the nonredundant space
    Mn = M[:numnonred,:numnonred]
    
    # 4. Solve the linear equation
    dK_short = np.linalg.solve(Mn, kn)
    dK = np.zeros(namps)
    
    # 5. Update radius    
    dK_short_norm = np.linalg.norm(dK_short)
    if (dK_short_norm > update_radius):
        dK_normalized = update_radius * dK_short/dK_short_norm
        for x in range(numnonred):
            dK[x] = dK_normalized[x]
    else:
        for x in range(numnonred):
            dK[x] = dK_short[x]
    
    # 6. Calculate dT and set the new amplitudes.
    dT = X @ dK
    
    if (diis is not None):
        t_old = copy.deepcopy(t)
        for x in range(len(t)):
            t[x] -= dT[x]
        t = diis.update(t, t_old)
    else:
        #print("No DIIS")
        for x in range(len(t)):
            t[x] -= dT[x]
        
    t_proj = P @ t
    op.set_coefficients(list(t_proj))
    
def orthogonalization(ic_basis_full, thres=1e-6, distribution_print=False, const_num_op=False, num_op=0):
    # Evangelista and Gauss: full-orthogonalization.
    ic_basis = ic_basis_full[1:]
    namps = len(ic_basis)
    
    S = np.zeros((namps, namps))
    for ibasis, i in enumerate(ic_basis):
        for jbasis, j in enumerate(ic_basis):
            S[ibasis,jbasis] = forte.overlap(i,j)
            S[jbasis,ibasis] = S[ibasis,jbasis]
    eigval, eigvec = np.linalg.eigh(S)
    
    if (distribution_print):  
        intervals = [(10, 1), (1, 1e-1), (1e-1, 1e-2), (1e-2, 1e-3), (1e-3, 1e-4),
                (1e-4, 1e-5), (1e-5, 1e-6), (1e-6, 1e-7), (1e-7, 1e-8),
                (1e-8, 1e-9), (1e-9, 1e-10), (1e-10, 1e-11), (1e-11, 1e-12),
                (1e-12, 1e-13), (1e-13, 1e-14), (1e-14, 1e-15)]
        
        interval_counts = {interval: 0 for interval in intervals}

        for val in eigval:
            for interval in intervals:
                if interval[0] > val >= interval[1]:
                    interval_counts[interval] += 1
        
        for interval, count in interval_counts.items():
            print(f"Interval {interval}: {count} eigenvalues")
    
    if (const_num_op):
        numnonred = num_op
        X = np.zeros((namps, namps))
        U = eigvec[:,-numnonred:]
        S_diag_large = np.diag(1./np.sqrt(eigval[-numnonred:]))
        X[:,:numnonred] = U @ S_diag_large
        P = U @ U.T
    else:
        numnonred = len(eigval[eigval > thres])
        X = np.zeros((namps, namps))
        U = eigvec[:,eigval > thres]
        S_diag_large = np.diag(1./np.sqrt(eigval[eigval > thres]))
        X[:,:numnonred] = U @ S_diag_large
        P = U @ U.T
    
    return P, S, X, numnonred

def orthogonalization_projective(ic_basis_full, num_op, thres=1e-6): 
    # The ic_basis_full must bave block structure.
    # This is the sequential orthogonalization.
    ic_basis_1 = ic_basis_full[1:num_op[0]+1]
    ic_basis_2 = ic_basis_full[num_op[0]+1:]
    namps_1 = len(ic_basis_1)
    namps_2 = len(ic_basis_2)
    namps = namps_1 + namps_2
    S = np.zeros((namps, namps))
    for ibasis, i in enumerate(ic_basis_full[1:]):
        for jbasis, j in enumerate(ic_basis_full[1:]):
            S[ibasis,jbasis] = forte.overlap(i,j)
            S[jbasis,ibasis] = S[ibasis,jbasis]
    X = np.zeros((namps, namps))
    
    # 1. Orthogonalize the single excitation block.
    S1 = S[:namps_1,:namps_1].copy()
    eigval1, eigvec1 = np.linalg.eigh(S1)
    numnonred1 = len(eigval1[eigval1 > thres])
    X1 = np.zeros((namps, namps_1))
    U1_short = eigvec1[:,eigval1 > thres]
    U1 = np.zeros((namps, numnonred1))
    U1[:namps_1, :numnonred1] = U1_short.copy()
    S1_diag_large = np.diag(1./np.sqrt(eigval1[eigval1 > thres]))
    X1[:namps_1, :numnonred1] = U1_short @ S1_diag_large
    
    # 2. Construct the Q matrix.
    Q = np.identity(namps)
    Q -= X1 @ X1.T @ S
    
    # 3. Construct the new S matrix with single excitation operatoras projected out.
    S2 = Q.T @ S @ Q
    
    # 4. Construct transformation matrix for the double excitation block.
    eigval2, eigvec2 = np.linalg.eigh(S2)
    U2 = eigvec2[:,eigval2 > thres]
    S2_diag_large = np.diag(1./np.sqrt(eigval2[eigval2 > thres]))
    U2 = Q @ U2
    X2 = U2 @ S2_diag_large
    
    # 5. Concatenate the two transformation matrices.
    X12 = np.concatenate((X1,X2),axis=1)
    U = np.concatenate((U1,U2),axis=1)
    numnonred = X12.shape[1]
    X[:,:numnonred] = X12.copy()
    
    # 6. Construct the projection matrix.
    P = U @ U.T
    
    # 7. Test the orthogonality.
    # test_close = test_orthogonalization(X, S, numnonred)
    # print(f'Orthogonalization test: {test_close}')
    
    return P, S, X, numnonred 

def orthogonalization_GNO(ic_basis_full, Y, thres=1e-6):
    ic_basis = ic_basis_full[1:]
    namps = len(ic_basis)
    # 1. Construct metric matrix.
    S = np.zeros((namps, namps))
    for ibasis, i in enumerate(ic_basis):
        for jbasis, j in enumerate(ic_basis):
            S[ibasis,jbasis] = forte.overlap(i,j)
            S[jbasis,ibasis] = S[ibasis,jbasis]
    
    # 2. Tranform the metric matrix in the basis of GNO excitation operators.  
    S_GNO = Y.T @ S @ Y 
    eigval, eigvec = np.linalg.eigh(S_GNO)
    
    # 3. Construct transformed P and X matrices.
    numnonred = len(eigval[eigval > thres])
    X = np.zeros((namps, namps))
    U = eigvec[:,eigval > thres]
    #U = Y @ U # Transformed U matrix
    
    S_diag_large = np.diag(1./np.sqrt(eigval[eigval > thres]))
    X[:,:numnonred] = Y @ U @ S_diag_large # Transformed X matrix
    Y_inv = np.linalg.inv(Y)
    P = Y @ U @ U.T @ Y_inv  # Transformed P matrix 
    return P, S, X, numnonred

def test_orthogonalization(X, S, numnonred):
    XSX = X.T @ S @ X
    I = np.zeros_like(S)
    np.fill_diagonal(I[:numnonred,:numnonred], 1.0)
    test_close = np.allclose(XSX, I)
    return test_close
    

def sym_dir_prod(occ_list, sym_list):
    # This function is used to calculate the symmetry of a specific excitation operator.
    if (len(occ_list) == 0): 
        return 0
    elif (len(occ_list) == 1):
        return sym_list[occ_list[0]]
    else:
        return functools.reduce(lambda i, j:  i ^ j, [sym_list[x] for x in occ_list])

def num_act(d, act_set):
    n = 0
    for i in d:
        if (i[1] in act_set):
            n += 1
    return n

class EOM_MRCC:
    def __init__(self, mos_spaces, wfn_cas, sym=0, max_exc=2, unitary=True, verbose=False, maxk=19, screen_thresh_H=0.0, screen_thresh_exp=1e-12, ortho='direct', const_num_op=False, add_int=False, cas_int=False, commutator=False, n_comm=2):
        self.forte_objs = forte.utils.prepare_forte_objects(wfn_cas,mos_spaces)
        self.mos_spaces = mos_spaces
        self.ints = self.forte_objs['ints']
        self.as_ints = self.forte_objs['as_ints']
        self.scf_info = self.forte_objs['scf_info']
        self.mo_space_info = self.forte_objs['mo_space_info']

        self.verbose = verbose
        self.maxk = maxk
        self.screen_thresh_H = screen_thresh_H
        self.screen_thresh_exp = screen_thresh_exp
        
        # Define MO spaces.
        self.occ = self.mo_space_info.corr_absolute_mo('GAS1')
        self.act = self.mo_space_info.corr_absolute_mo('GAS2')
        self.vir = self.mo_space_info.corr_absolute_mo('GAS3')
        self.all_orb = self.mo_space_info.corr_absolute_mo('CORRELATED')
        
        if (self.verbose): print(f'{self.occ=}')
        if (self.verbose): print(f'{self.act=}')
        if (self.verbose): print(f'{self.vir=}')
        if (self.verbose): print(f'{self.all_orb=}')
        
        self.hole = self.occ + self.act
        self.particle = self.act + self.vir
        if (self.verbose): print(f'{self.hole=}')
        if (self.verbose): print(f'{self.particle=}')
        
        self.max_exc = max_exc
        
        self.unitary = unitary
        
        # Obtain symmetry information.
        self.sym = sym # target symmetry
        self.act_sym = self.mo_space_info.symmetry('GAS2')
        self.vir_sym = self.mo_space_info.symmetry('GAS3')
        self.all_sym = self.mo_space_info.symmetry('CORRELATED') 
        self.nirrep = self.mo_space_info.nirrep()
        self.nmopi = wfn_cas.nmopi()
        if (self.verbose): print(f'{self.act_sym=}')
        if (self.verbose): print(f'{self.all_sym=}')
        if (self.verbose): print(f'{self.vir_sym=}')

        self.nael = wfn_cas.nalpha()
        self.nbel = wfn_cas.nbeta()
        
        # Obtain 1-RDM.
        _frozen_docc = self.mos_spaces['FROZEN_DOCC'] if 'FROZEN_DOCC' in self.mos_spaces else [0]*self.nirrep
        mos_spaces_rdm = {
                          'FROZEN_DOCC' : _frozen_docc,
                          'RESTRICTED_DOCC' : mos_spaces['GAS1'],
                          'ACTIVE' : mos_spaces['GAS2'],
                         }
        self.ints_rdms = forte.utils.prepare_ints_rdms(wfn_cas,mos_spaces_rdm,rdm_level=2)
        self.rdms = self.ints_rdms['rdms']
        self.gamma1, self.gamma2 = forte.spinorbital_rdms(self.rdms)
        
        # Construct generalized Fock matrix, obtain orbital energies.
        self.get_fock_block()
        
        self.ea = []
        self.eb = []
        
        for i in range(self.f.shape[0]):
            if (i % 2 == 0):
                self.ea.append(self.f[i,i])
            else:
                self.eb.append(self.f[i,i])
                
        if (self.verbose): print(f'{self.ea=}')
        if (self.verbose): print(f'{self.ea=}')
        
        # Some additional parameters.
        self.ortho = ortho
        self.const_num_op=const_num_op 
        self.add_int = add_int
        self.cas_int = cas_int
        self.commutator = commutator
        self.n_comm = n_comm

    def get_fock_block(self):
        self.f = forte.spinorbital_oei(self.ints, self.all_orb, self.all_orb)
        v = forte.spinorbital_tei(self.ints,self.all_orb,self.occ,self.all_orb,self.occ)
        self.f += np.einsum('piqi->pq', v)
        v = forte.spinorbital_tei(self.ints,self.all_orb,self.act,self.all_orb,self.act)
        self.f += np.einsum('piqj,ij->pq', v, self.gamma1) 
        
    def get_casci_wfn(self, nelecas):
        # This function is used to obtain the CASCI wave function.
        self.dets = []
        corbs = self.mo_space_info.corr_absolute_mo('GAS1') 
        aorbs = self.mo_space_info.corr_absolute_mo('GAS2')
        aorbs_rel = range(len(aorbs))

        if (self.verbose): print(f'{corbs=}')
        if (self.verbose): print(f'{aorbs=}')
        nact_ael = nelecas[0]
        nact_bel = nelecas[1]

        for astr in itertools.combinations(aorbs_rel, nact_ael):
            asym = sym_dir_prod(astr, self.act_sym)
            for bstr in itertools.combinations(aorbs_rel, nact_bel):
                bsym = sym_dir_prod(bstr, self.act_sym)
                if (asym ^ bsym == self.sym):
                    d = forte.Determinant()
                    for i in corbs: d.set_alfa_bit(i, True)
                    for i in corbs: d.set_beta_bit(i, True)
                    for i in astr: d.set_alfa_bit(aorbs[i], True)
                    for i in bstr: d.set_beta_bit(aorbs[i], True)
                    self.dets.append(d)
        
        ndets = len(self.dets)
        print(f'Number of determinants: {ndets}')
        H = np.zeros((ndets,ndets))
        for i in range(len(self.dets)):
            for j in range(i+1):
                H[i,j] = self.as_ints.slater_rules(self.dets[i],self.dets[j])

        # print(H)
        evals_casci, evecs_casci = np.linalg.eigh(H, 'L')

        e_casci = evals_casci[0] + self.as_ints.scalar_energy() + self.as_ints.nuclear_repulsion_energy()
        print(f'CASCI Energy = {e_casci}')
        print(evals_casci+ self.as_ints.scalar_energy() + self.as_ints.nuclear_repulsion_energy())
        
        c_casci_0 = evecs_casci[:,0]
        
        #Get the reference CASCI state.
        self.psi = forte.StateVector(dict(zip(self.dets, c_casci_0)))

        _frozen_docc = self.mos_spaces['FROZEN_DOCC'] if 'FROZEN_DOCC' in self.mos_spaces else [0]*self.nirrep
        mos_spaces_fci = {
                          'FROZEN_DOCC' : _frozen_docc,
                          'GAS1' : [0]*self.nirrep, 
                          'GAS2' : list(np.array(self.nmopi.to_tuple()) - np.array(_frozen_docc)),
                          'GAS3' : [0]*self.nirrep,
                         }

        forte_objs_fci = forte.utils.prepare_forte_objects(wfn_cas,mos_spaces_fci)
        as_ints_fci = forte_objs_fci['as_ints']
        mo_space_info_fci = forte_objs_fci['mo_space_info']
        
        # Below is for FCI calculation. Super slow, just for testing.
        # ===============================================================================================
        if (False):
            self.dets_fci = []
            corbs_fci = []
            aorbs_fci = mo_space_info_fci.absolute_mo('GAS2')
            ncore_fci = 0
            nact_ael_fci = self.nael
            nact_bel_fci = self.nbel
            aorbs_rel_fci = range(len(aorbs_fci))
            for astr in itertools.combinations(aorbs_rel_fci, nact_ael_fci):
                asym = sym_dir_prod(astr, self.all_sym)
                for bstr in itertools.combinations(aorbs_rel_fci, nact_bel_fci):
                    bsym = sym_dir_prod(bstr, self.all_sym)
                    if (asym ^ bsym == self.sym):
                        d = forte.Determinant()
                        for i in corbs_fci: d.set_alfa_bit(i, True)
                        for i in corbs_fci: d.set_beta_bit(i, True)
                        for i in astr: d.set_alfa_bit(aorbs_fci[i], True)
                        for i in bstr: d.set_beta_bit(aorbs_fci[i], True)
                        self.dets_fci.append(d)
            
            ndets_fci = len(self.dets_fci)
            self.H_fci = np.zeros((ndets_fci,ndets_fci))
            for i in range(len(self.dets_fci)):
                for j in range(i+1):
                    self.H_fci[i,j] = as_ints_fci.slater_rules(self.dets_fci[i],self.dets_fci[j])
                    if (i == j):
                        self.H_fci[i,j] += as_ints_fci.scalar_energy() + as_ints_fci.nuclear_repulsion_energy()
            
            evals_fci, evecs_fci = np.linalg.eigh(self.H_fci, 'L')
            
            s2 = np.zeros((len(self.dets_fci),)*2)
            for i in range(len(self.dets_fci)):
                for j in range(len(self.dets_fci)):
                    s2[i,j] = forte.spin2(self.dets_fci[i],self.dets_fci[j])
            n_sin = 0
            n_tri = 0
            for i in range(len(evals_fci)):
                ci = evecs_fci[:,i]
                print(abs(ci.T @ s2 @ ci))
                if (abs(ci.T @ s2 @ ci) < 1.0):
                    n_sin += 1
                    print(f'FCI {n_sin} singlet energy: {evals_fci[i]}')
                else:
                    n_tri += 1
                    print(f'FCI {n_tri} triplet energy: {evals_fci[i]}')
        # ===============================================================================================

        self.ham_op = forte.SparseHamiltonian(as_ints_fci)
        self.exp_op = forte.SparseExp()
        self.as_ints_fci = as_ints_fci
        
    def CAS_INT(self, nelecas, internal_max_exc):
        # This function is used to test whether internally contracted CASCI is a good approximation to the full CASCI.
        self.cas_int_basis = [self.psi]
        _op_idx_single = []
        _op_idx_double = []
        diag_1 = []
        diag_2 = []
       
        for n in range(1,internal_max_exc + 1):
            # loop over beta excitation level
            max_nb = min(n,nelecas[1])
            for nb in range(max_nb+1):
                # We should at least have two electrons in active space.
                na = n - nb
                # loop over alpha occupied
                for ao in itertools.combinations(self.act, na):
                    ao_sym = sym_dir_prod(ao, self.all_sym)
                    # loop over alpha virtual
                    for av in itertools.combinations(self.act, na):
                        av_sym = sym_dir_prod(av, self.all_sym)
                        # loop over beta occupied
                        for bo in itertools.combinations(self.act, nb):
                            bo_sym = sym_dir_prod(bo, self.all_sym)
                            # loop over beta virtual
                            for bv in itertools.combinations(self.act, nb):
                                bv_sym = sym_dir_prod(bv, self.all_sym)
                                if (ao_sym ^ av_sym ^ bo_sym ^ bv_sym == self.sym):
                                    T_op_temp = forte.SparseOperator(antihermitian=False)
                                    l = [] # a list to hold the operator triplets
                                    for i in ao: l.append((False,True,i)) # alpha occupied
                                    for i in bo: l.append((False,False,i)) # beta occupied        
                                    for a in reversed(bv): l.append((True,False,a)) # beta virtual                                                                    
                                    for a in reversed(av): l.append((True,True,a)) # alpha virtual
                                    
                                    T_op_temp.add_term(l,1.0, allow_reordering=False) # No reordering in principle.
                                    self.cas_int_basis.append(forte.apply_operator(T_op_temp,self.psi))
                                    
                                    idx = []
                                    for item in l:
                                        idx.append((item[1],item[2])) # (spin, orbital)
                                        
                                    if (n == 1):
                                        _op_idx_single.append(idx)
                                        r1_idx_1 = 0
                                        r1_idx_2 = 0
                                        if (l[0][1] == True):
                                            r1_idx_1 = 2 * self.act.index(l[0][2])
                                        else:
                                            r1_idx_1 = 2 * self.act.index(l[0][2]) + 1
                                        if (l[1][1] == True):
                                            r1_idx_2 = 2 * self.act.index(l[1][2])
                                        else:
                                            r1_idx_2 = 2 * self.act.index(l[1][2]) + 1
                                        diag_1.append(-self.gamma1[r1_idx_1,r1_idx_2])
                                        
                                    elif (n == 2):
                                        _op_idx_double.append(idx)
                                        r2_idx_1 = 0
                                        r2_idx_2 = 0
                                        r2_idx_3 = 0
                                        r2_idx_4 = 0
                                        if (l[0][1] == True):
                                            r2_idx_1 = 2 * self.act.index(l[0][2])
                                        else:
                                            r2_idx_1 = 2 * self.act.index(l[0][2]) + 1
                                        if (l[1][1] == True):
                                            r2_idx_2 = 2 * self.act.index(l[1][2])
                                        else:
                                            r2_idx_2 = 2 * self.act.index(l[1][2]) + 1
                                        if (l[2][1] == True):
                                            r2_idx_3 = 2 * self.act.index(l[2][2])
                                        else:
                                            r2_idx_3 = 2 * self.act.index(l[2][2]) + 1
                                        if (l[3][1] == True):
                                            r2_idx_4 = 2 * self.act.index(l[3][2])
                                        else:
                                            r2_idx_4 = 2 * self.act.index(l[3][2]) + 1
                                        diag_2.append(- self.gamma2[r2_idx_4,r2_idx_3,r2_idx_1,r2_idx_2] - 2 * self.gamma1[r2_idx_1, r2_idx_3]*self.gamma1[r2_idx_2, r2_idx_4] + 2 * self.gamma1[r2_idx_1, r2_idx_4]*self.gamma1[r2_idx_2, r2_idx_3])
        
        _nsingle = len(diag_1)
        _ndouble = len(diag_2)
        
        print("start GNO")
                
        _P_full = np.zeros((len(self.act)*2,len(self.act)*2,len(self.act)*2,len(self.act)*2,len(self.act)*2,len(self.act)*2))
        _I_act = np.identity(len(self.act)*2)

        _act_idx = {i:idx for idx,i in enumerate(self.act)}
        _act_spin_idx = lambda s: _act_idx[s[1]]*2 if s[0] else _act_idx[s[1]]*2+1

        _P_full += np.einsum('ik,ab,jc->ibcajk', _I_act, _I_act, self.gamma1,optimize='optimal')
        _P_full -= np.einsum('ik,ac,jb->ibcajk', _I_act, _I_act, self.gamma1,optimize='optimal')
        _P_full += np.einsum('ij,ac,kb->ibcajk', _I_act, _I_act, self.gamma1,optimize='optimal')
        _P_full -= np.einsum('ij,ab,kc->ibcajk', _I_act, _I_act, self.gamma1,optimize='optimal')

        
        GNO_P_sub = np.zeros((_nsingle,_ndouble))
        for isingle, s in enumerate(_op_idx_single):
            for idouble, d in enumerate(_op_idx_double):
                GNO_P_sub[isingle, idouble] = _P_full[_act_spin_idx(s[0]),_act_spin_idx(d[3]),_act_spin_idx(d[2]),_act_spin_idx(s[1]),_act_spin_idx(d[0]),_act_spin_idx(d[1])]  
        
        diag_total = diag_1 + diag_2
        GNO_P = np.identity(1 + _nsingle + _ndouble)
        GNO_P[1:_nsingle+1,1+_nsingle:] = GNO_P_sub.copy()
        GNO_P[0,1:] = np.array(diag_total).copy()
        
        print("end GNO")
                                    
        cas_int_H = np.zeros((len(self.cas_int_basis),len(self.cas_int_basis)))
        for i in range(len(self.cas_int_basis)):
            for j in range(i+1):
                Hwfn = self.ham_op.compute(self.cas_int_basis[i], self.screen_thresh_H)
                cas_int_H[j,i] = forte.overlap(self.cas_int_basis[j], Hwfn)
                cas_int_H[i,j] = cas_int_H[j,i]
        cas_int_H_GNO = GNO_P.T @ cas_int_H @ GNO_P
                
        S = np.zeros((len(self.cas_int_basis),len(self.cas_int_basis)))
        for i in range(len(self.cas_int_basis)):
            for j in range(i+1):
                S[i,j] = forte.overlap(self.cas_int_basis[i], self.cas_int_basis[j])
                S[j,i] = S[i,j]
                
        S = GNO_P.T @ S @ GNO_P
        eigval, eigvec = np.linalg.eigh(S)
        
        S_squareroot = np.diag(1./np.sqrt(eigval[eigval > 1e-9]))
        U = eigvec[:,eigval > 1e-9]
        
        X_tilde = U @ S_squareroot

        cas_int_H_GNO = X_tilde.T @ cas_int_H_GNO @ X_tilde
        print(f'internal space: {cas_int_H_GNO.shape[0]}')
        evals_cas_int, evecs_cas_int = np.linalg.eigh(cas_int_H_GNO)
        
        print(evals_cas_int)
                                       
        
    
    def Harper_test(self, nelecas):
        # A test for Harper R. Grimsley's paper. Not for my project.
        self.dets = []
        corbs = self.mo_space_info.corr_absolute_mo('GAS1') 
        aorbs = self.mo_space_info.corr_absolute_mo('GAS2')
        aorbs_rel = range(len(aorbs))

        if (self.verbose): print(f'{corbs=}')
        if (self.verbose): print(f'{aorbs=}')
        nact_ael = nelecas[0]
        nact_bel = nelecas[1]

        for astr in itertools.combinations(aorbs_rel, nact_ael):
            asym = sym_dir_prod(astr, self.act_sym)
            for bstr in itertools.combinations(aorbs_rel, nact_bel):
                bsym = sym_dir_prod(bstr, self.act_sym)
                if (asym ^ bsym == self.sym):
                    d = forte.Determinant()
                    for i in corbs: d.set_alfa_bit(i, True)
                    for i in corbs: d.set_beta_bit(i, True)
                    for i in astr: d.set_alfa_bit(aorbs[i], True)
                    for i in bstr: d.set_beta_bit(aorbs[i], True)
                    self.dets.append(d)
        
        ndets = len(self.dets)
        H = np.zeros((ndets,ndets))
        for i in range(len(self.dets)):
            for j in range(i+1):
                H[i,j] = self.as_ints.slater_rules(self.dets[i],self.dets[j])

        # print(H)
        evals_casci, evecs_casci = np.linalg.eigh(H, 'L')

        e_casci = evals_casci[0] + self.as_ints.scalar_energy() + self.as_ints.nuclear_repulsion_energy()
        print(f'CASCI Energy = {e_casci}')
        
        a1_det = []
        b2_det = []
        
        for astr in itertools.combinations(aorbs_rel, nact_ael):
            asym = sym_dir_prod(astr, self.act_sym)
            for bstr in itertools.combinations(aorbs_rel, nact_bel):
                bsym = sym_dir_prod(bstr, self.act_sym)
                if (asym == 0 and bsym == 0):
                    d = forte.Determinant()
                    for i in corbs: d.set_alfa_bit(i, True)
                    for i in corbs: d.set_beta_bit(i, True)
                    for i in astr: d.set_alfa_bit(aorbs[i], True)
                    for i in bstr: d.set_beta_bit(aorbs[i], True)
                    a1_det.append(d)
                elif (asym == 3 and bsym == 3):
                    d = forte.Determinant()
                    for i in corbs: d.set_alfa_bit(i, True)
                    for i in corbs: d.set_beta_bit(i, True)
                    for i in astr: d.set_alfa_bit(aorbs[i], True)
                    for i in bstr: d.set_beta_bit(aorbs[i], True)
                    b2_det.append(d)
        print(a1_det)
        print(b2_det)
        
        a1_energy = self.as_ints.slater_rules(a1_det[0],a1_det[0]) + self.as_ints.scalar_energy() + self.as_ints.nuclear_repulsion_energy()
        b2_energy = self.as_ints.slater_rules(b2_det[0],b2_det[0]) + self.as_ints.scalar_energy() + self.as_ints.nuclear_repulsion_energy()
        print(f'a1 energy = {a1_energy}')
        print(f'b2 energy = {b2_energy}')        
        

    def initialize_op(self):
        # Initialize excitation operators.
        self.op_A = forte.SparseOperator(antihermitian=True) # For MRUCC
        self.op_T = forte.SparseOperator(antihermitian=False) # For MRCC
        self.oprator_list = []
        self.denominators = []
        self.ic_basis = [self.psi]
        
        self.num_op = np.zeros(self.max_exc, dtype=int) # The number of operators for each rank.
        
        self.op_idx = []
        
        self.flip = []

        # loop over total excitation level
        for n in range(1,self.max_exc + 1):
            # loop over beta excitation level
            for nb in range(n + 1):
                na = n - nb
                # loop over alpha occupied
                for ao in itertools.combinations(self.hole, na):
                    ao_sym = sym_dir_prod(ao, self.all_sym)
                    # loop over alpha virtual
                    for av in itertools.combinations(self.particle, na):
                        av_sym = sym_dir_prod(av, self.all_sym)
                        # loop over beta occupied
                        for bo in itertools.combinations(self.hole, nb):
                            bo_sym = sym_dir_prod(bo, self.all_sym)
                            # loop over beta virtual
                            for bv in itertools.combinations(self.particle, nb):
                                bv_sym = sym_dir_prod(bv, self.all_sym)
                                if (ao_sym ^ av_sym ^ bo_sym ^ bv_sym == self.sym):
                                    # create an operator from a list of tuples (creation, alpha, orb) where
                                    #   creation : bool (true = creation, false = annihilation)
                                    #   alpha    : bool (true = alpha, false = beta)
                                    #   orb      : int  (the index of the mo)
                                    l = [] # a list to hold the operator triplets
                                    for i in ao: l.append((False,True,i)) # alpha occupied
                                    for i in bo: l.append((False,False,i)) # beta occupied
                                    for a in reversed(bv): l.append((True,False,a)) # beta virtual
                                    for a in reversed(av): l.append((True,True,a)) # alpha virtual
                                    all_in_act = all(item[2] in self.act for item in l)
                                    if (not all_in_act or self.add_int):
                                        A_op_temp = forte.SparseOperator(antihermitian=True)
                                        T_op_temp = forte.SparseOperator(antihermitian=False)
                                        # compute the denominatorsr                                 
                                        e_aocc = 0.0
                                        e_avir = 0.0
                                        e_bocc = 0.0
                                        e_bvir = 0.0
                                        for i in ao: e_aocc += self.ea[i]
                                        for i in av: e_avir += self.ea[i]
                                        for i in bo: e_bocc += self.eb[i]
                                        for i in bv: e_bvir += self.eb[i]
                                        # Reorder l to act, occ, vir, act. Only for double excitations.
                                        if (self.ortho == 'GNO'):
                                            num_act = 0
                                            for item in l:
                                                if item[2] in self.act:
                                                    num_act += 1
                                            if (n == 2 and num_act >= 2):
                                                num_act_o = 0
                                                num_act_v = 0
                                                pos_act_o = []
                                                pos_act_v = []
                                                for idx, item in enumerate(l[:2]):
                                                    if (item[2] in self.act):
                                                        num_act_o += 1
                                                        pos_act_o.append(idx)
                                                for idx, item in enumerate(l[2:]):
                                                    if (item[2] in self.act):
                                                        num_act_v += 1
                                                        pos_act_v.append(idx)
                                                if (num_act_o == 1):
                                                    if (pos_act_o[0] == 0):
                                                        continue
                                                    else:
                                                        l[:2] = [l[1], l[0]]
                                                if (num_act_v == 1):
                                                    if (pos_act_v[0] == 1):
                                                        continue
                                                    else:
                                                        l[2:] = [l[3], l[2]]
                                            
                                        
                                        idx = []
                                        for item in l:
                                            idx.append((item[1],item[2])) # (spin, orbital)
                                        self.op_idx.append(idx)

                                        denom = e_aocc + e_bocc - e_bvir - e_avir
                                        self.denominators.append(denom)
                                        A_op_temp.add_term(l,1.0, allow_reordering=True)
                                        T_op_temp.add_term(l,1.0, allow_reordering=True)
                                        
                                        if (T_op_temp.coefficients()[0] < 0.0):
                                            coeff = [1.0] * T_op_temp.size()
                                            T_op_temp.set_coefficients(coeff)
                                            self.flip.append(-1.0)
                                        else:
                                            self.flip.append(1.0)
                                            
                                        self.num_op[n-1] += 1
                                        self.ic_basis.append(forte.apply_operator(T_op_temp,self.psi))
                                        self.oprator_list.append(T_op_temp)
                                        self.op_A.add_term(l,1.0, allow_reordering=True) # a_{ij..}^{ab..} * (t_{ij..}^{ab..} - t_{ab..}^{ij..})
                                        self.op_T.add_term(l,1.0, allow_reordering=True)
        
        if (self.ortho == 'GNO'):
            _Y_full = np.zeros((len(self.hole)*2,len(self.particle)*2,len(self.particle)*2,len(self.particle)*2,len(self.hole)*2,len(self.hole)*2))
            _I_occ = np.identity(len(self.occ)*2)
            _I_act = np.identity(len(self.act)*2)
            _I_vir = np.identity(len(self.vir)*2)
            _ho = slice(0,len(self.occ)*2)
            _ha = slice(len(self.occ)*2,len(self.occ)*2+len(self.act)*2)
            _pa = slice(0,len(self.act)*2)
            _pv = slice(len(self.act)*2,len(self.act)*2+len(self.vir)*2)

            _hole_idx = {i:idx for idx,i in enumerate(self.hole)}
            _particle_idx = {i:idx for idx,i in enumerate(self.particle)}
            _hole_spin_idx = lambda s: _hole_idx[s[1]]*2 if s[0] else _hole_idx[s[1]]*2+1
            _particle_spin_idx = lambda s: _particle_idx[s[1]]*2 if s[0] else _particle_idx[s[1]]*2+1

            _Y_full[_ho,_pa,_pv,_pv,_ha,_ho] -= np.einsum('ij,ba,vu->ivbauj', _I_occ, _I_vir, self.gamma1)
            _Y_full[_ha,_pa,_pv,_pv,_ha,_ha] -= np.einsum('ba,uw,xv->uxbavw', _I_vir, _I_act, self.gamma1)
            _Y_full[_ha,_pa,_pv,_pv,_ha,_ha] += np.einsum('ba,uv,xw->uxbavw', _I_vir, _I_act, self.gamma1)
            _Y_full[_ho,_pa,_pa,_pa,_ha,_ho] -= np.einsum('ij,xu,wv->iwxuvj', _I_occ, _I_act, self.gamma1)
            _Y_full[_ho,_pa,_pa,_pa,_ha,_ho] += np.einsum('ij,wu,xv->iwxuvj', _I_occ, _I_act, self.gamma1)

            GNO_Y_sub = np.zeros((self.num_op[0],self.num_op[1]))
            for isingle, s in enumerate(self.op_idx[:self.num_op[0]]):
                for idouble, d in enumerate(self.op_idx[self.num_op[0]:]):
                    GNO_Y_sub[isingle, idouble] = _Y_full[_hole_spin_idx(s[0]),_particle_spin_idx(d[3]),_particle_spin_idx(d[2]),_particle_spin_idx(s[1]),_hole_spin_idx(d[0]),_hole_spin_idx(d[1])]  
            
            for i in range(self.num_op[1]):
                GNO_Y_sub[:,i] *= self.flip[i+self.num_op[0]]
                                
            self.GNO_Y = np.identity(self.num_op[0]+self.num_op[1])
            
            self.GNO_Y[:self.num_op[0],self.num_op[0]:] = GNO_Y_sub.copy()
        

        self.denominators = np.array(self.denominators)

        if (self.verbose): print(f'Number of IC basis functions: {len(self.ic_basis)}')
        if (self.verbose): print(f'Breakdown: {self.num_op}')    
            
    def run_ic_mrcc_variational(self, t0=0.01):
        if (self.unitary): 
            op = self.op_A
        else:
            op = self.op_T
        self.t = [t0] * op.size()
        res = scipy.optimize.minimize(fun=cc_variational_functional, x0=self.t, \
                                      args=(op,self.psi,self.ham_op,self.exp_op,\
                                            self.screen_thresh_H,self.screen_thresh_exp,self.maxk),\
                                      method='BFGS')
        print(res)

    def run_ic_mrcc(self, e_convergence=1.e-12, max_cc_iter=500, eta=0.1, thres=1e-6, algo='oprod', num_op=0):
        start = time.time()
        if (self.unitary): 
            op = self.op_A
        else:
            op = self.op_T

        ic_basis = self.ic_basis  # This is a full ic_basis which contains psi_casci.

        # initialize T = 0
        self.t = [0.0] * op.size()
        op.set_coefficients(self.t)
        
        diis = DIIS(self.t, diis_start=3)
        #diis = None

        # initalize E = 0
        old_e = 0.0

        print('=================================================================')
        print('   Iteration     Energy (Eh)       Delta Energy (Eh)    Time (s)')   
        print('-----------------------------------------------------------------')   
        if (self.ortho == 'direct'):     
            P, S, X, numnonred = orthogonalization(ic_basis, thres=thres, const_num_op=self.const_num_op, num_op=num_op)
        elif (self.ortho == 'projective'):
            P, S, X, numnonred = orthogonalization_projective(ic_basis, self.num_op, thres=thres)
        elif (self.ortho == 'GNO'):
            P, S, X, numnonred = orthogonalization_GNO(ic_basis, self.GNO_Y, thres=thres)
        
        radius = 0.01
        
        for iter in range(max_cc_iter):
            # 1. evaluate the CC residual equations.
            if (self.commutator):
                self.residual, self.e = cc_residual_equations_truncated(op,self.psi,self.ham_op,self.screen_thresh_H, n_comm=self.n_comm) # Truncated BCH expansion.
            else:
                self.residual, self.e = cc_residual_equations(op,self.psi,self.ham_op,self.exp_op,self.maxk,self.screen_thresh_H,self.screen_thresh_exp) # Full BCH expansion.
            if (self.e - old_e) > 0.0:
                if (radius > 1e-7):
                    radius /= 2.0 
            
            # 2. update the CC equations
            update_amps_orthogonal(self.residual,self.denominators, op, self.t, P, S, X, numnonred, update_radius=radius, eta=eta, diis=diis)
            
            # 3. Form Heff
            Heff = np.zeros((len(self.dets),len(self.dets)))
            if (self.commutator):
                _wfn_map_full = []
                _Hwfn_map_full = []
                for i in range(len(self.dets)):
                    idet = forte.StateVector({self.dets[i]:1.0})
                    wfn_comm = idet
                    Hwfn_comm = self.ham_op.compute(wfn_comm,self.screen_thresh_H)
                    _wfn_list = [wfn_comm]
                    _Hwfn_list = [Hwfn_comm]
                    for ik in range(self.n_comm):
                        wfn_comm = forte.apply_operator(op,wfn_comm)
                        Hwfn_comm = self.ham_op.compute(wfn_comm,self.screen_thresh_H)
                        _wfn_list.append(wfn_comm)
                        _Hwfn_list.append(Hwfn_comm)
                        
                    _wfn_map_full.append(_wfn_list)
                    _Hwfn_map_full.append(_Hwfn_list)
                
                for i in range(len(self.dets)):
                    for j in range(i+1):  
                        energy = 0.0
                        for k in range(self.n_comm+1):
                            for l in range(k+1):
                                m = k - l
                                right_wfn = _Hwfn_map_full[i][m]
                                left_wfn = _wfn_map_full[j][l]
                                energy += forte.overlap(left_wfn,right_wfn) / (math.factorial(l) * math.factorial(m))
                        Heff[j,i] = energy  
                        Heff[i,j] = energy            
            else:
                if (algo == 'naive'):
                    Heff = np.zeros((len(self.dets),len(self.dets)))
                    for i in range(len(self.dets)):
                        for j in range(len(self.dets)):
                            idet = forte.StateVector({self.dets[i]:1.0})
                            jdet = forte.StateVector({self.dets[j]:1.0})
                            wfn = self.exp_op.compute(op,jdet,scaling_factor=1.0,maxk=self.maxk,screen_thresh=self.screen_thresh_exp)
                            Hwfn = self.ham_op.compute(wfn,self.screen_thresh_H)
                            R = self.exp_op.compute(op,Hwfn,scaling_factor=-1.0,maxk=self.maxk,screen_thresh=self.screen_thresh_exp)
                            Heff[i,j] = forte.overlap(idet,R)
                if (algo == 'oprod'):
                    _wfn_list = []
                    _Hwfn_list = []

                    for i in range(len(self.dets)):
                        idet = forte.StateVector({self.dets[i]:1.0})
                        wfn = self.exp_op.compute(op,idet,scaling_factor=1.0,maxk=self.maxk,screen_thresh=self.screen_thresh_exp)
                        Hwfn = self.ham_op.compute(wfn,self.screen_thresh_H)
                        _wfn_list.append(wfn)
                        _Hwfn_list.append(Hwfn)

                    for i in range(len(self.dets)):
                        for j in range(len(self.dets)):
                            Heff[i,j] = forte.overlap(_wfn_list[i],_Hwfn_list[j])
                            Heff[j,i] = Heff[i,j]
                    
            w, vr = scipy.linalg.eig(Heff)
            vr = np.real(vr)
            idx = np.argmin(np.real(w))
            self.psi = forte.StateVector(dict(zip(self.dets, vr[:,idx])))
            
            ic_basis_new = [self.psi]
            for x in range(len(self.oprator_list)):
                ic_basis_new.append(forte.apply_operator(self.oprator_list[x],self.psi))
        
            if (self.ortho == 'direct'):     
                P, S, X, numnonred = orthogonalization(ic_basis_new, thres=thres)
            elif (self.ortho == 'projective'):
                P, S, X, numnonred = orthogonalization_projective(ic_basis_new, self.num_op, thres=thres)
            elif (self.ortho == 'GNO'): 
                P, S, X, numnonred = orthogonalization_GNO(ic_basis_new, self.GNO_Y, thres=thres)
                
            # 4. print information
            print(f'{iter:9d} {self.e:20.12f} {self.e - old_e:20.12f} {time.time() - start:11.3f}')   
                
            # 5. check for convergence of the energy
            self.ic_basis = ic_basis_new.copy()
            if abs(self.e - old_e) < e_convergence:
                print('=================================================================')
                print(f' ic-MRCCSD energy: {self.e:20.12f} [Eh]')
                P, S, X, numnonred = orthogonalization(ic_basis_new, thres=thres, distribution_print=True)
                print(f'Number of selected operators for ic-MRCCSD: {numnonred}')
                
                print(f'Number of possible CAS internal: {len(w)-1}')
                
                if (self.cas_int):
                    for i in range(len(w)):
                        if i != idx:
                            cas_psi = forte.StateVector(dict(zip(self.dets, vr[:,i])))
                            self.ic_basis.append(cas_psi)
                    print(f' Number of ic_basis for EOM_UMRCC (CAS Internal): {len(self.ic_basis)}')
                
                break
            old_e = self.e

    def run_eom_ee_mrcc(self, nelecas, thres=1e-6, decontract_active=False, internal_max_exc=2, algo='oprod', num_op_eom=0):
        # Separate single and double ic_basis.
        self.ic_basis_single = [self.psi]
        self.ic_basis_double = []
        for x in range(self.num_op[0]):
            self.ic_basis_single.append(forte.apply_operator(self.oprator_list[x],self.psi))
        for y in range(self.num_op[1]):
            self.ic_basis_double.append(forte.apply_operator(self.oprator_list[y+self.num_op[0]],self.psi))
            
        diag_1 = list(np.zeros(len(self.ic_basis_single))) # With Psi.
        diag_2 = list(np.zeros(len(self.ic_basis_double)))
        
        if (not self.add_int and not self.cas_int):
            if (decontract_active):
                for i in self.dets:
                    self.ic_basis.append(forte.StateVector({i:1.0}))
            else:
                print("Use internally internal excitations. No GNO yet.")
                
                self.op_idx_single = self.op_idx[:self.num_op[0]]
                self.op_idx_double = self.op_idx[self.num_op[0]:]
                self.flip_single = self.flip[:self.num_op[0]]
                self.flip_double = self.flip[self.num_op[0]:]
                
                for n in range(1,internal_max_exc + 1):
                    # loop over beta excitation level
                    max_nb = min(n,nelecas[1])
                    for nb in range(max_nb+1):
                        # We should at least have two electrons in active space.
                        na = n - nb
                        # loop over alpha occupied
                        for ao in itertools.combinations(self.act, na):
                            ao_sym = sym_dir_prod(ao, self.all_sym)
                            # loop over alpha virtual
                            for av in itertools.combinations(self.act, na):
                                av_sym = sym_dir_prod(av, self.all_sym)
                                # loop over beta occupied
                                for bo in itertools.combinations(self.act, nb):
                                    bo_sym = sym_dir_prod(bo, self.all_sym)
                                    # loop over beta virtual
                                    for bv in itertools.combinations(self.act, nb):
                                        bv_sym = sym_dir_prod(bv, self.all_sym)
                                        if (ao_sym ^ av_sym ^ bo_sym ^ bv_sym == self.sym):
                                            T_op_temp = forte.SparseOperator(antihermitian=False)
                                            l = [] # a list to hold the operator triplets
                                            for i in ao: l.append((False,True,i)) # alpha occupied
                                            for i in bo: l.append((False,False,i)) # beta occupied        
                                            for a in reversed(bv): l.append((True,False,a)) # beta virtual                                                                    
                                            for a in reversed(av): l.append((True,True,a)) # alpha virtual
                                            
                                            T_op_temp.add_term(l,1.0, allow_reordering=False) # No reordering in principle.
                                            
                                            idx = []
                                            for item in l:
                                                idx.append((item[1],item[2])) # (spin, orbital)
                                                
                                            if (n == 1):
                                                self.op_idx_single.append(idx)
                                                self.flip_single.append(1.0)
                                                self.ic_basis_single.append(forte.apply_operator(T_op_temp,self.psi))  
                                                r1_idx_1 = 0
                                                r1_idx_2 = 0
                                                if (l[0][1] == True):
                                                    r1_idx_1 = 2 * self.act.index(l[0][2])
                                                else:
                                                    r1_idx_1 = 2 * self.act.index(l[0][2]) + 1
                                                if (l[1][1] == True):
                                                    r1_idx_2 = 2 * self.act.index(l[1][2])
                                                else:
                                                    r1_idx_2 = 2 * self.act.index(l[1][2]) + 1
                                                diag_1.append(-self.gamma1[r1_idx_1,r1_idx_2])
                                                
                                            elif (n == 2):
                                                self.op_idx_double.append(idx)
                                                self.flip_double.append(1.0)
                                                self.ic_basis_double.append(forte.apply_operator(T_op_temp,self.psi))
                                                r2_idx_1 = 0
                                                r2_idx_2 = 0
                                                r2_idx_3 = 0
                                                r2_idx_4 = 0
                                                if (l[0][1] == True):
                                                    r2_idx_1 = 2 * self.act.index(l[0][2])
                                                else:
                                                    r2_idx_1 = 2 * self.act.index(l[0][2]) + 1
                                                if (l[1][1] == True):
                                                    r2_idx_2 = 2 * self.act.index(l[1][2])
                                                else:
                                                    r2_idx_2 = 2 * self.act.index(l[1][2]) + 1
                                                if (l[2][1] == True):
                                                    r2_idx_3 = 2 * self.act.index(l[2][2])
                                                else:
                                                    r2_idx_3 = 2 * self.act.index(l[2][2]) + 1
                                                if (l[3][1] == True):
                                                    r2_idx_4 = 2 * self.act.index(l[3][2])
                                                else:
                                                    r2_idx_4 = 2 * self.act.index(l[3][2]) + 1
                                                diag_2.append(- self.gamma2[r2_idx_4,r2_idx_3,r2_idx_1,r2_idx_2] - 2 * self.gamma1[r2_idx_1, r2_idx_3]*self.gamma1[r2_idx_2, r2_idx_4] + 2 * self.gamma1[r2_idx_1, r2_idx_4]*self.gamma1[r2_idx_2, r2_idx_3])
                                                
                n_single = len(self.ic_basis_single)-1 # The first one is psi.
                n_double = len(self.ic_basis_double)   
                
                n_single_int = n_single - self.num_op[0]
                n_double_int = n_double - self.num_op[1]
                
                self.ic_basis = self.ic_basis_single + self.ic_basis_double
                
                print(f'Number of used internal: {n_single_int+n_double_int, n_single_int, n_double_int}')
                  
                if (self.verbose): print(f'Number of EOM basis function without psi (breakdown): {n_single, n_double}')  
                
                self.op_idx = self.op_idx_single + self.op_idx_double
                self.flip = self.flip_single + self.flip_double
                                                
                # Generalized normal ordering.
                print("GNO starts.")
                self.gamma1_hp = np.zeros((len(self.hole)*2,len(self.particle)*2))
                self.gamma1_hp[len(self.occ)*2:, :len(self.act)*2] = self.gamma1.copy()
                
                _P_full = np.zeros((len(self.hole)*2,len(self.particle)*2,len(self.particle)*2,len(self.particle)*2,len(self.hole)*2,len(self.hole)*2))
                _I_hole = np.identity(len(self.hole)*2)
                _I_particle = np.identity(len(self.particle)*2)

                _hole_idx = {i:idx for idx,i in enumerate(self.hole)}
                _particle_idx = {i:idx for idx,i in enumerate(self.particle)}
                _hole_spin_idx = lambda s: _hole_idx[s[1]]*2 if s[0] else _hole_idx[s[1]]*2+1
                _particle_spin_idx = lambda s: _particle_idx[s[1]]*2 if s[0] else _particle_idx[s[1]]*2+1

                _P_full += np.einsum('ik,ab,jc->ibcajk', _I_hole, _I_particle, self.gamma1_hp)
                _P_full -= np.einsum('ik,ac,jb->ibcajk', _I_hole, _I_particle, self.gamma1_hp)
                _P_full += np.einsum('ij,ac,kb->ibcajk', _I_hole, _I_particle, self.gamma1_hp)
                _P_full -= np.einsum('ij,ab,kc->ibcajk', _I_hole, _I_particle, self.gamma1_hp)

                GNO_P_sub = np.zeros((n_single,n_double))
                for isingle, s in enumerate(self.op_idx[:n_single]):
                    for idouble, d in enumerate(self.op_idx[n_single:]):
                        GNO_P_sub[isingle, idouble] = _P_full[_hole_spin_idx(s[0]),_particle_spin_idx(d[3]),_particle_spin_idx(d[2]),_particle_spin_idx(s[1]),_hole_spin_idx(d[0]),_hole_spin_idx(d[1])]  
                
                for i in range(n_double):
                    GNO_P_sub[:,i] *= self.flip[i+n_single]
                
                diag_total = diag_1 + diag_2
                diag_total[0] = 1.0
                self.GNO_P = np.identity(len(self.ic_basis))
                self.GNO_P[1:n_single+1,1+n_single:] = GNO_P_sub.copy()
                self.GNO_P[0,:] = np.array(diag_total).copy()
                
                print("GNO ends.")
    
        print(f' Number of ic_basis for EOM_UMRCC: {len(self.ic_basis)}')
        
        if (self.commutator):
            self.get_hbar_commutator()
        else:
            if (algo == 'matmul'):
                self.get_hbar_matmul()
            elif (algo == 'naive'):
                self.get_hbar_naive()
            elif (algo == 'oprod'):
                self.get_hbar_oprod()
            
        
        S_full = np.zeros((len(self.ic_basis), len(self.ic_basis)))
        for ibasis, i in enumerate(self.ic_basis):
            for jbasis, j in enumerate(self.ic_basis):
                S_full[ibasis,jbasis] = forte.overlap(i,j)
                S_full[jbasis,ibasis] = S_full[ibasis,jbasis]
        
        if (not self.add_int and not self.cas_int):
            print("Now do transformation to GNO basis.")
            S_full = self.GNO_P.T @ S_full @ self.GNO_P
            self.Hbar_ic = self.GNO_P.T @ self.Hbar_ic @ self.GNO_P
        
        eigval, eigvec = np.linalg.eigh(S_full)
        
        intervals = [(10, 1), (1, 1e-1), (1e-1, 1e-2), (1e-2, 1e-3), (1e-3, 1e-4),
                (1e-4, 1e-5), (1e-5, 1e-6), (1e-6, 1e-7), (1e-7, 1e-8),
                (1e-8, 1e-9), (1e-9, 1e-10), (1e-10, 1e-11), (1e-11, 1e-12),
                (1e-12, 1e-13), (1e-13, 1e-14), (1e-14, 1e-15)]
        
        interval_counts = {interval: 0 for interval in intervals}

        for val in range(len(eigval)):
            print(f"EOM {val} Eigenvalue: {eigval[val]}")
            for interval in intervals:
                if interval[0] > eigval[val] >= interval[1]:
                    interval_counts[interval] += 1
        
        for interval, count in interval_counts.items():
            print(f"EOM Interval {interval}: {count} eigenvalues")
            
        
        numnonred = 0
        S = np.array([0])
        U = np.array([0])
        
        if (self.const_num_op):
            numnonred = num_op_eom
            print(eigval[-numnonred:])
            S = np.diag(1./np.sqrt(eigval[-numnonred:]))
            U = eigvec[:,-numnonred:]
        else:
            numnonred = len(eigval[eigval > thres])  
            S = np.diag(1./np.sqrt(eigval[eigval > thres]))
            U = eigvec[:,eigval > thres]
        
        print(f'Number of selected operators for EOM-UMRCCSD: {numnonred}')
        X_tilde = U @ S


        H_ic_tilde = X_tilde.T @ self.Hbar_ic @ X_tilde
        eval_ic, evec_ic = np.linalg.eigh(H_ic_tilde)

        s2 = np.zeros((len(self.ic_basis),)*2)
        for i in range(len(self.ic_basis)):
            for j in range(len(self.ic_basis)):
                for di, coeff_i in self.ic_basis[i].items():
                    for dj, coeff_j in self.ic_basis[j].items():
                        s2[i,j] += forte.spin2(di,dj)*coeff_i*coeff_j

        c_total = X_tilde @ evec_ic
        
        norm = np.zeros((len(evec_ic)))
        for i in range(len(eval_ic)):
            norm[i] = c_total[:,i].T @ S_full @ c_total[:,i]
            
        np.save("Hbar_ic.npy", self.Hbar_ic)
        np.save("H_ic_tilde.npy", H_ic_tilde)
        
        n_sin = 0
        n_tri = 0
        n_quintet = 0
        for i in range(len(eval_ic)):
            c0_per = 0.0
            single_per = 0.0
            double_per = 0.0
            single_internal = 0.0
            double_internal = 0.0
            total_per = 0.0
            ci = c_total[:,i]
            for s in range(1, self.num_op[0]+1):
                single_per += ci[s]**2
            for id in range(self.num_op[0]+1, self.num_op[0]+ n_single_int+1):
                single_internal += ci[id]**2
            for ic in range(self.num_op[0]+ n_single_int+1, self.num_op[0]+ n_single_int + 1 + self.num_op[1]):
                double_per += ci[ic]**2
            for ii in range(self.num_op[0]+ n_single_int + 1 + self.num_op[1], len(ci)):
                double_internal += ci[ii]**2  
            c0_per += ci[0]**2
            for it in range(len(ci)):
                total_per += ci[it]**2
            c0_per /= total_per
            single_internal /= total_per
            double_internal /= total_per
            single_per /= total_per
            double_per /= total_per
            print(abs(ci.T @ s2 @ ci)/norm[i])
            if (abs(ci.T @ s2 @ ci)/norm[i] < 1.0):
                n_sin += 1
                print(f' EOM-UMRCCSD {n_sin} singlet energy: {eval_ic[i]:20.12f} [Eh] c0_per: {c0_per*100:0.2f}, single_int: {single_internal*100:0.2f}, double_int: {double_internal*100:0.2f}, single_per: {single_per*100:0.2f}, double_per: {double_per*100:0.2f}')
            elif (1.0 < abs(ci.T @ s2 @ ci)/norm[i] < 3.0):
                n_tri += 1
                print(f' EOM-UMRCCSD {n_tri} triplet energy: {eval_ic[i]:20.12f} [Eh] c0_per: {c0_per*100:0.2f}, single_int: {single_internal*100:0.2f}, double_int: {double_internal*100:0.2f}, single_per: {single_per*100:0.2f}, double_per: {double_per*100:0.2f}')
        
            elif (4.0 < abs(ci.T @ s2 @ ci)/norm[i] < 7.0):
                n_quintet += 1
                print(f' EOM-UMRCCSD {n_quintet} quintet energy: {eval_ic[i]:20.12f} c0_per: {c0_per*100:0.2f}, single_int: {single_internal*100:0.2f}, double_int: {double_internal*100:0.2f}, single_per: {single_per*100:0.2f}, double_per: {double_per*100:0.2f}')
            else: # Change this.
                print(f' EOM-UMRCCSD {i} energy: {eval_ic[i]:20.12f} [Eh] c0_per: {c0_per*100:0.2f}, single_int: {single_internal*100:0.2f}, double_int: {double_internal*100:0.2f}, single_per: {single_per*100:0.2f}, double_per: {double_per*100:0.2f}')
                
        
    def get_ic_coeff(self):
        self.ic_coeff = np.zeros((len(self.dets_fci), len(self.ic_basis)))
        self.dets_fci = np.array(self.dets_fci)

        for j in range(len(self.ic_basis)):
            for d, coeff in self.ic_basis[j].items():
                loc = np.where(self.dets_fci == d)
                self.ic_coeff[loc,j] = coeff
                
    def get_hbar_commutator(self):
        _wfn_map_full = []
        _Hwfn_map_full = []
        self.Hbar_ic = np.zeros((len(self.ic_basis),)*2)
        for ibasis in range(len(self.ic_basis)):
            i = self.ic_basis[ibasis]
            wfn_comm = i
            Hwfn_comm = self.ham_op.compute(wfn_comm,self.screen_thresh_H)
            _wfn_list = [wfn_comm]
            _Hwfn_list = [Hwfn_comm]
            for ik in range(self.n_comm):
                wfn_comm = forte.apply_operator(self.op_A,wfn_comm)
                Hwfn_comm = self.ham_op.compute(wfn_comm,self.screen_thresh_H)
                _wfn_list.append(wfn_comm)
                _Hwfn_list.append(Hwfn_comm)
                
            _wfn_map_full.append(_wfn_list)
            _Hwfn_map_full.append(_Hwfn_list)
            
        for ibasis in range(len(self.ic_basis)):    
            for jbasis in range(ibasis+1):
                energy = 0.0
                for k in range(self.n_comm+1):
                    for l in range(k+1):
                        m = k - l
                        right_wfn = _Hwfn_map_full[ibasis][m]
                        left_wfn = _wfn_map_full[jbasis][l]
                        energy += forte.overlap(left_wfn,right_wfn) / (math.factorial(l) * math.factorial(m))
                self.Hbar_ic[jbasis,ibasis] = energy  
                self.Hbar_ic[ibasis,jbasis] = energy 

    def get_hbar_matmul(self):
        if (self.unitary): 
            op = self.op_A
        else:
            op = self.op_T
        
        ndets_fci = len(self.dets_fci)
        self.amp_elements = np.zeros((ndets_fci,ndets_fci))
        for i in range(len(self.dets_fci)):
            for j in range(i+1):
                wfn = forte.apply_operator(op,forte.StateVector({self.dets_fci[j]:1.0}))
                self.amp_elements[i,j] = forte.overlap(forte.StateVector({self.dets_fci[i]:1.0}),wfn)
                if (i!=j):
                    self.H_fci[j,i] = self.H_fci[i,j]
                    self.amp_elements[j,i] = -self.amp_elements[i,j]
                    
        self.Hbar_ic = scipy.linalg.expm(-1.0*self.amp_elements) @ self.H_fci @ scipy.linalg.expm(self.amp_elements)
        self.Hbar_ic = self.ic_coeff.T @ self.Hbar_ic @ self.ic_coeff

    def get_hbar_naive(self):
        self.Hbar_ic = np.zeros((len(self.ic_basis),)*2)
        for ibasis in range(len(self.ic_basis)):
            for jbasis in range(len(self.ic_basis)):
                i = self.ic_basis[ibasis]
                j = self.ic_basis[jbasis]
                wfn = self.exp_op.compute(self.op_A,i,scaling_factor=1.0,maxk=self.maxk,screen_thresh=self.screen_thresh_exp)
                Hwfn = self.ham_op.compute(wfn,self.screen_thresh_H)
                Heffwfn = self.exp_op.compute(self.op_A,Hwfn,scaling_factor=-1.0,maxk=self.maxk,screen_thresh=self.screen_thresh_exp)
                self.Hbar_ic[ibasis,jbasis] = forte.overlap(j,Heffwfn)
    
    def get_hbar_oprod(self):
        self.Hbar_ic = np.zeros((len(self.ic_basis),)*2)
        _wfn_list = []
        _Hwfn_list = []

        for ibasis in range(len(self.ic_basis)):
            i = self.ic_basis[ibasis]
            wfn = self.exp_op.compute(self.op_A,i,scaling_factor=1.0,maxk=self.maxk,screen_thresh=self.screen_thresh_exp)
            Hwfn = self.ham_op.compute(wfn,self.screen_thresh_H)
            _wfn_list.append(wfn)
            _Hwfn_list.append(Hwfn)

        for i in range(len(self.ic_basis)):
            for j in range(len(self.ic_basis)):
                self.Hbar_ic[i,j] = forte.overlap(_wfn_list[i],_Hwfn_list[j])
                self.Hbar_ic[j,i] = self.Hbar_ic[i,j]

class DIIS:
    """A class that implements DIIS for CC theory 

        Parameters
    ----------
    diis_start : int
        Start the iterations when the DIIS dimension is greather than this parameter (default = 3)
    """
    def __init__(self, t, diis_start=3):
        self.t_diis = [t]
        self.e_diis = []
        self.diis_start = diis_start

    def update(self, t, t_old):
        """Update the DIIS object and return extrapolted amplitudes

        Parameters
        ----------
        t : list
            The updated amplitudes
        t_old : list
            The previous set of amplitudes            
        Returns
        -------
        list
            The extrapolated amplitudes
        """

        if self.diis_start == -1:
            return t

        self.t_diis.append(t)
        self.e_diis.append(np.subtract(t, t_old))

        diis_dim = len(self.t_diis) - 1
        if (diis_dim >= self.diis_start) and (diis_dim < len(t)):
            # consturct diis B matrix (following Crawford Group github tutorial)
            B = np.ones((diis_dim + 1, diis_dim + 1)) * -1.0
            bsol = np.zeros(diis_dim + 1)
            B[-1, -1] = 0.0
            bsol[-1] = -1.0
            for i in range(len(self.e_diis)):
                for j in range(i, len(self.e_diis)):
                    B[i, j] = np.dot(np.real(self.e_diis[i]), np.real(self.e_diis[j]))
                    if i != j:
                        B[j, i] = B[i, j]
            B[:-1, :-1] /= np.abs(B[:-1, :-1]).max()
            x = np.linalg.solve(B, bsol)
            t_new = np.zeros((len(t)))
            for l in range(diis_dim):
                temp_ary = x[l] * np.asarray(self.t_diis[l + 1])
                t_new = np.add(t_new, temp_ary)
            return copy.deepcopy(list(np.real(t_new)))

        return t            


if __name__ == "__main__":
        test = 2
        if (test == 1):
            # Frozen core test.
            psi4.core.set_output_file('beh_output.dat', False)
            x = 1.000
            mol = psi4.geometry(f"""
            Be 0.0   0.0             0.0
            H  {x}   {2.54-0.46*x}   0.0
            H  {x}  -{2.54-0.46*x}   0.0
            symmetry c2v
            units bohr
            """)
            
            psi4.set_options({
                'basis': 'sto-6g',
                'frozen_docc': [0,0,0,0],
                'restricted_docc':[2,0,0,0],
                
                'reference': 'rhf',
            })
            
            forte_options = {
                'basis': 'sto-6g',
                'job_type': 'mcscf_two_step',
                'active_space_solver': 'fci',
                'frozen_docc': [0,0,0,0],
                'restricted_docc':[2,0,0,0],
                'active':[1,0,0,1],
                'root_sym': 0,
                'maxiter': 100,
                'e_convergence': 1e-8,
                'r_convergence': 1e-8,
                'casscf_e_convergence': 1e-8,
                'casscf_g_convergence': 1e-6,
            }

            E_casscf, wfn_cas = psi4.energy('forte', forte_options=forte_options, return_wfn=True)
            
            print(f'CASSCF Energy = {E_casscf}')
        
            mos_spaces = {'FROZEN_DOCC' : [1,0,0,0],
                        'GAS1' : [1,0,0,0], 
                        'GAS2' : [1,0,0,1],
                        'GAS3' : [1,0,1,1]
                        }

            ic_mrcc = EOM_MRCC(mos_spaces, wfn_cas, verbose=True,maxk=8,screen_thresh_H=1e-8,screen_thresh_exp=1e-8)
            ic_mrcc.get_casci_wfn([1,1]) 
            ic_mrcc.initialize_op()
            ic_mrcc.run_ic_mrcc(e_convergence=1e-9,eta=-1.0,thres=1e-6,algo='oprod')
            #ic_mrcc.run_eom_ee_mrcc([1,1], thres=1e-6, algo='oprod')
            
        elif (test == 2):
            # Symmetry test: edge case: 1 CAS electron per spin
            psi4.core.set_output_file('beh_output.dat', False)
            x = 1.000
            mol = psi4.geometry(f"""
            Be 0.0   0.0             0.0
            H  {x}   {2.54-0.46*x}   0.0
            H  {x}  -{2.54-0.46*x}   0.0
            symmetry c2v
            units bohr
            """)
            
            psi4.set_options({
                'basis': 'sto-6g',
                'frozen_docc': [0,0,0,0],
                'restricted_docc':[2,0,0,0],
                'reference': 'rhf',
            })
            
            forte_options = {
                'basis': 'sto-6g',
                'job_type': 'mcscf_two_step',
                'active_space_solver': 'fci',
                'frozen_docc': [0,0,0,0],
                'restricted_docc':[2,0,0,0],
                'active':[1,0,0,1],
                'root_sym': 0,
                'maxiter': 100,
                'e_convergence': 1e-8,
                'r_convergence': 1e-8,
                'casscf_e_convergence': 1e-8,
                'casscf_g_convergence': 1e-6,
            }

            E_casscf, wfn_cas = psi4.energy('forte', forte_options=forte_options, return_wfn=True)
            
            print(f'CASSCF Energy = {E_casscf}')
        
            mos_spaces = {'GAS1' : [2,0,0,0], 
                        'GAS2' : [1,0,0,1],
                        'GAS3' : [1,0,1,1]
                        }

            ic_mrcc = EOM_MRCC(mos_spaces, wfn_cas, verbose=True,maxk=8,screen_thresh_H=1e-12,screen_thresh_exp=1e-8, ortho='direct', add_int=False, cas_int=False, commutator=False, n_comm=2)
            ic_mrcc.get_casci_wfn([1,1]) 
            ic_mrcc.initialize_op()
            ic_mrcc.run_ic_mrcc(e_convergence=1e-9,max_cc_iter=200,eta=-1.0,thres=1e-4,algo='oprod')
            ic_mrcc.run_eom_ee_mrcc([1,1], internal_max_exc=2, thres=1e-4, algo='oprod')
            
        elif (test == 3):
            # Variational test.
            psi4.core.set_output_file('beh_output.dat', False)
            x = 1.000
            mol = psi4.geometry(f"""
            Be 0.0   0.0             0.0
            H  {x}   {2.54-0.46*x}   0.0
            H  {x}  -{2.54-0.46*x}   0.0
            symmetry c2v
            units bohr
            """)
            
            psi4.set_options({
                'basis': 'sto-6g',
                'frozen_docc': [0,0,0,0],
                'restricted_docc':[2,0,0,0],
                'reference': 'rhf',
            })
            
            forte_options = {
                'basis': 'sto-6g',
                'job_type': 'mcscf_two_step',
                'active_space_solver': 'fci',
                'frozen_docc': [0,0,0,0],
                'restricted_docc':[2,0,0,0],
                'active':[1,0,0,1],
                'root_sym': 0,
                'maxiter': 100,
                'e_convergence': 1e-8,
                'r_convergence': 1e-8,
                'casscf_e_convergence': 1e-8,
                'casscf_g_convergence': 1e-6,
            }

            E_casscf, wfn_cas = psi4.energy('forte', forte_options=forte_options, return_wfn=True)
            
            print(f'CASSCF Energy = {E_casscf}')
        
            mos_spaces = {'GAS1' : [2,0,0,0], 
                        'GAS2' : [1,0,0,1],
                        'GAS3' : [1,0,1,1]
                        }

            ic_mrcc = EOM_MRCC(mos_spaces, wfn_cas, verbose=True,maxk=8,screen_thresh_H=1e-8,screen_thresh_exp=1e-8)
            ic_mrcc.get_casci_wfn([1,1]) 
            ic_mrcc.initialize_op()
            ic_mrcc.run_ic_mrcc_variational()